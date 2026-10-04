// GameCube pixel pipeline on an emulated EFB (see gxrender.rs).
//
// * vs_tev / fs_tev: one GX draw batch. Positions arrive as homogeneous clip coordinates over
//   the whole EFB; the TEV stages run per pixel in the GameCube's gamma space and the result is
//   written (and blended, by the fixed-function blend state) into the EFB colour target.
// * vs_full + fs_copy: an EFB copy (BP 0x52) into a texture: source rectangle, optional 2x2
//   box filter, colour or depth source, and the copy format's channel conversion.
// * vs_clear + fs_clear: the clear that follows a copy with the clear bit.
// * vs_full + fs_blit / fs_blit_srgb: the copied XFB to the window.

struct Tev {
    stages: array<vec4<u32>, 16>,
    regs: array<vec4<f32>, 4>,
    konst: array<vec4<f32>, 4>,
    // x = stages, y = alpha compare (BP 0xF3), z = swap tables, w = unused
    params: vec4<u32>,
    // x = A, y = C, z = B magnitude, w = B shift
    fog: vec4<f32>,
    // rgb = fog colour, w = fsel | proj << 3
    fog_color: vec4<f32>,
    // Logical texture map sizes: map 2k in [k].xy, map 2k+1 in [k].zw (texture coordinates
    // arrive in texels: normalised x the setup unit's texcoord scale).
    tex_size: array<vec4<f32>, 4>,
    // x = indirect stages, y = RAS1_IREF, z = RAS1_SS0, w = RAS1_SS1
    ind: vec4<u32>,
    // Indirect command per TEV stage (BP 0x10 + stage).
    ind_cmd: array<vec4<u32>, 4>,
    // Indirect matrices 0..2 as their three BP registers each (0x06..0x0E).
    ind_mtx: array<vec4<u32>, 3>,
};

@group(0) @binding(0) var<uniform> tev: Tev;
@group(0) @binding(1) var tex0: texture_2d<f32>;
@group(0) @binding(2) var samp0: sampler;
@group(0) @binding(3) var tex1: texture_2d<f32>;
@group(0) @binding(4) var samp1: sampler;
@group(0) @binding(5) var tex2: texture_2d<f32>;
@group(0) @binding(6) var samp2: sampler;
@group(0) @binding(7) var tex3: texture_2d<f32>;
@group(0) @binding(8) var samp3: sampler;
@group(0) @binding(9) var tex4: texture_2d<f32>;
@group(0) @binding(10) var samp4: sampler;
@group(0) @binding(11) var tex5: texture_2d<f32>;
@group(0) @binding(12) var samp5: sampler;
@group(0) @binding(13) var tex6: texture_2d<f32>;
@group(0) @binding(14) var samp6: sampler;
@group(0) @binding(15) var tex7: texture_2d<f32>;
@group(0) @binding(16) var samp7: sampler;

// Texture coordinates 0..7, two per attribute (xy = even, zw = odd).
struct Vertex {
    @location(0) position: vec4<f32>,
    @location(1) color0: vec4<f32>,
    @location(2) color1: vec4<f32>,
    @location(3) uv01: vec4<f32>,
    @location(4) uv23: vec4<f32>,
    @location(5) uv45: vec4<f32>,
    @location(6) uv67: vec4<f32>,
};

struct VOut {
    @builtin(position) clip: vec4<f32>,
    @location(0) color0: vec4<f32>,
    @location(1) color1: vec4<f32>,
    @location(2) uv01: vec4<f32>,
    @location(3) uv23: vec4<f32>,
    @location(4) uv45: vec4<f32>,
    @location(5) uv67: vec4<f32>,
};

@vertex
fn vs_tev(v: Vertex) -> VOut {
    var o: VOut;
    o.clip = v.position;
    o.color0 = v.color0;
    o.color1 = v.color1;
    o.uv01 = v.uv01;
    o.uv23 = v.uv23;
    o.uv45 = v.uv45;
    o.uv67 = v.uv67;
    return o;
}

// Sample texture map `m` (0..7) with explicit gradients, so it can be called from the
// non-uniform stage loop.
fn sample_map(m: u32, uv: vec2<f32>, gx: vec2<f32>, gy: vec2<f32>) -> vec4<f32> {
    switch (m) {
        case 0u: { return textureSampleGrad(tex0, samp0, uv, gx, gy); }
        case 1u: { return textureSampleGrad(tex1, samp1, uv, gx, gy); }
        case 2u: { return textureSampleGrad(tex2, samp2, uv, gx, gy); }
        case 3u: { return textureSampleGrad(tex3, samp3, uv, gx, gy); }
        case 4u: { return textureSampleGrad(tex4, samp4, uv, gx, gy); }
        case 5u: { return textureSampleGrad(tex5, samp5, uv, gx, gy); }
        case 6u: { return textureSampleGrad(tex6, samp6, uv, gx, gy); }
        default: { return textureSampleGrad(tex7, samp7, uv, gx, gy); }
    }
}

fn map_size(m: u32) -> vec2<f32> {
    let v = tev.tex_size[m >> 1u];
    if ((m & 1u) == 0u) { return max(v.xy, vec2<f32>(1.0)); }
    return max(v.zw, vec2<f32>(1.0));
}

fn ind_cmd(s: u32) -> u32 {
    return tev.ind_cmd[s >> 2u][s & 3u];
}

fn ind_mtx_reg(k: u32) -> u32 {
    return tev.ind_mtx[k >> 2u][k & 3u];
}

// Sign-extended 11-bit indirect matrix element.
fn s11(v: u32) -> i32 {
    return (i32(v & 0x7FFu) << 21u) >> 21u;
}

// Shift a s.7 fixed-point offset by the matrix scale: 17 - scale (right) or left if negative.
fn ind_shift(v: vec2<i32>, shift: i32) -> vec2<i32> {
    if (shift >= 0) {
        return v >> vec2<u32>(u32(shift));
    }
    return v << vec2<u32>(u32(-shift) & 31u);
}

// Indirect wrap of one s.7 coordinate: 0 off, 1..5 = 256 >> (n - 1) texels, 6 = to zero.
fn ind_wrap(c: i32, w: u32) -> i32 {
    if (w == 0u) { return c; }
    if (w >= 6u) { return 0; }
    let size = (256 >> (w - 1u)) << 7u;
    return c & (size - 1);
}

// Apply swap table `tbl` (each table: 2-bit source component for R, G, B, A).
fn swap(c: vec4<f32>, tbl: u32) -> vec4<f32> {
    let p = (tev.params.z >> (8u * tbl)) & 0xFFu;
    var v = c;
    return vec4<f32>(v[p & 3u], v[(p >> 2u) & 3u], v[(p >> 4u) & 3u], v[(p >> 6u) & 3u]);
}

fn konst_sel(sel: u32) -> vec4<f32> {
    if (sel < 8u) {
        return vec4<f32>(f32(8u - sel) / 8.0);
    }
    if (sel >= 0x0Cu && sel <= 0x0Fu) {
        return tev.konst[sel - 0x0Cu];
    }
    if (sel >= 0x10u) {
        let k = tev.konst[sel & 3u];
        let comp = (sel - 0x10u) / 4u;
        return vec4<f32>(k[comp]);
    }
    return vec4<f32>(1.0);
}

fn bias_scale(r_in: f32, env: u32) -> f32 {
    var r = r_in;
    let bias = (env >> 16u) & 3u;
    if (bias == 1u) { r = r + 0.5; }
    if (bias == 2u) { r = r - 0.5; }
    let scale = (env >> 20u) & 3u;
    if (scale == 1u) { r = r * 2.0; }
    if (scale == 2u) { r = r * 4.0; }
    if (scale == 3u) { r = r * 0.5; }
    if (((env >> 19u) & 1u) == 1u) {
        return clamp(r, 0.0, 1.0);
    }
    return clamp(r, -1024.0 / 255.0, 1023.0 / 255.0);
}

fn u8v(x: f32) -> u32 {
    return u32(round(clamp(x, 0.0, 1.0) * 255.0));
}

// Comparison-mode helpers (bias == 3): mode = scale << 1 | sub.
fn cmp_scalar(a: vec3<f32>, b: vec3<f32>, mode: u32) -> bool {
    let ar = u8v(a.r); let ag = u8v(a.g); let ab = u8v(a.b);
    let br = u8v(b.r); let bg = u8v(b.g); let bb = u8v(b.b);
    switch (mode >> 1u) {
        case 0u: { if ((mode & 1u) == 0u) { return ar > br; } return ar == br; }
        case 1u: {
            let av = (ag << 8u) | ar; let bv = (bg << 8u) | br;
            if ((mode & 1u) == 0u) { return av > bv; } return av == bv;
        }
        default: {
            let av = (ab << 16u) | (ag << 8u) | ar; let bv = (bb << 16u) | (bg << 8u) | br;
            if ((mode & 1u) == 0u) { return av > bv; } return av == bv;
        }
    }
}

fn alpha_test(a: u32, func: u32, refv: u32) -> bool {
    switch (func) {
        case 0u: { return false; }
        case 1u: { return a < refv; }
        case 2u: { return a == refv; }
        case 3u: { return a <= refv; }
        case 4u: { return a > refv; }
        case 5u: { return a != refv; }
        case 6u: { return a >= refv; }
        default: { return true; }
    }
}

@fragment
fn fs_tev(i: VOut) -> @location(0) vec4<f32> {
    // Texcoords and their screen-space gradients, taken in uniform control flow.
    let uvp = array<vec4<f32>, 4>(i.uv01, i.uv23, i.uv45, i.uv67);
    let dx = array<vec4<f32>, 4>(dpdx(i.uv01), dpdx(i.uv23), dpdx(i.uv45), dpdx(i.uv67));
    let dy = array<vec4<f32>, 4>(dpdy(i.uv01), dpdy(i.uv23), dpdy(i.uv45), dpdy(i.uv67));
    var regs = tev.regs; // PREV, C0, C1, C2
    let n = min(tev.params.x, 16u);

    // Indirect texture lookups (Dolphin's integer model): each indirect stage samples its map at
    // its texcoord in s.7 fixed point, scaled down by RAS1_SS, and yields (A, B, G) as 8-bit ints.
    var iind = array<vec3<i32>, 4>(vec3<i32>(0), vec3<i32>(0), vec3<i32>(0), vec3<i32>(0));
    let nind = min(tev.ind.x, 4u);
    for (var k = 0u; k < nind; k = k + 1u) {
        let imap = (tev.ind.y >> (6u * k)) & 7u;
        let itc = (tev.ind.y >> (6u * k + 3u)) & 7u;
        var ss = tev.ind.z;
        if (k >= 2u) { ss = tev.ind.w; }
        let sh = (k & 1u) * 8u;
        let half = itc >> 1u;
        var uvt = uvp[half].xy;
        if ((itc & 1u) == 1u) { uvt = uvp[half].zw; }
        let fp = vec2<i32>(uvt * 128.0);
        let crd = vec2<i32>(fp.x >> ((ss >> sh) & 15u), fp.y >> ((ss >> (sh + 4u)) & 15u));
        let t = sample_map(imap, vec2<f32>(crd) / (128.0 * map_size(imap)), vec2<f32>(0.0), vec2<f32>(0.0));
        iind[k] = vec3<i32>(round(vec3<f32>(t.a, t.b, t.g) * 255.0));
    }
    var tevcoord = vec2<i32>(0);
    for (var s = 0u; s < n; s = s + 1u) {
        let st = tev.stages[s];
        let cenv = st.x;
        let aenv = st.y;
        let order = st.z;
        let map = order & 7u;
        let tc = (order >> 4u) & 7u;
        let en = (order >> 7u) & 1u;
        let chan = (order >> 8u) & 7u;

        var tex = vec4<f32>(1.0);
        if (en == 1u) {
            let half = tc >> 1u;
            var uvt = uvp[half].xy;
            var gx = dx[half].xy;
            var gy = dy[half].xy;
            if ((tc & 1u) == 1u) {
                uvt = uvp[half].zw;
                gx = dx[half].zw;
                gy = dy[half].zw;
            }
            let size = map_size(map);
            let cmd = ind_cmd(s);
            if (cmd == 0u) {
                tex = sample_map(map, uvt / size, gx / size, gy / size);
                tevcoord = vec2<i32>(uvt * 128.0);
            } else {
                // Indirect stage: offset = matrix x (format-masked, biased) indirect texel.
                let fp = vec2<i32>(uvt * 128.0);
                let bt = cmd & 3u;
                let fmt = (cmd >> 2u) & 3u;
                let bias = (cmd >> 4u) & 7u;
                let bs = (cmd >> 7u) & 3u;
                let mid = (cmd >> 9u) & 15u;
                var trans = vec2<i32>(0);
                if (bt < nind && (bs != 0u || mid != 0u)) {
                    var c = iind[bt];
                    let masks = array<i32, 4>(0xFF, 0x1F, 0x0F, 0x07);
                    c = c & vec3<i32>(masks[fmt]);
                    var bv = 1;
                    if (fmt == 0u) { bv = -128; }
                    if ((bias & 1u) != 0u) { c.x = c.x + bv; }
                    if ((bias & 2u) != 0u) { c.y = c.y + bv; }
                    if ((bias & 4u) != 0u) { c.z = c.z + bv; }
                    let mi = mid & 3u;
                    if (mi != 0u) {
                        let ra = ind_mtx_reg(3u * (mi - 1u));
                        let rb = ind_mtx_reg(3u * (mi - 1u) + 1u);
                        let rc = ind_mtx_reg(3u * (mi - 1u) + 2u);
                        let scale = i32(((ra >> 22u) & 3u) | (((rb >> 22u) & 3u) << 2u) | (((rc >> 22u) & 3u) << 4u));
                        let shift = 17 - scale;
                        if (mid < 4u) {
                            let r0 = vec3<i32>(s11(ra), s11(rb), s11(rc));
                            let r1 = vec3<i32>(s11(ra >> 11u), s11(rb >> 11u), s11(rc >> 11u));
                            trans = ind_shift(vec2<i32>(dot(r0, c), dot(r1, c)) >> vec2<u32>(3u), shift);
                        } else if (mid < 8u) {
                            trans = ind_shift((fp * vec2<i32>(c.x)) >> vec2<u32>(8u), shift);
                        } else {
                            trans = ind_shift((fp * vec2<i32>(c.y)) >> vec2<u32>(8u), shift);
                        }
                    }
                }
                let wrapped = vec2<i32>(ind_wrap(fp.x, (cmd >> 13u) & 7u), ind_wrap(fp.y, (cmd >> 16u) & 7u));
                if (((cmd >> 20u) & 1u) != 0u) {
                    tevcoord = tevcoord + wrapped + trans;
                } else {
                    tevcoord = wrapped + trans;
                }
                // The texture unit's coordinates are s24.
                tevcoord = (tevcoord << vec2<u32>(8u)) >> vec2<u32>(8u);
                tex = sample_map(map, vec2<f32>(tevcoord) / (128.0 * size), gx / size, gy / size);
            }
        }
        tex = swap(tex, (aenv >> 2u) & 3u);
        var ras = vec4<f32>(0.0);
        if (chan == 0u) { ras = i.color0; }
        if (chan == 1u) { ras = i.color1; }
        ras = swap(ras, aenv & 3u);
        let kc = konst_sel(st.w & 31u);
        let ka = konst_sel((st.w >> 8u) & 31u).a;

        // Colour inputs: CPREV APREV C0 A0 C1 A1 C2 A2 TEXC TEXA RASC RASA ONE HALF KONST ZERO
        var cin = array<vec3<f32>, 16>(
            regs[0].rgb, vec3<f32>(regs[0].a), regs[1].rgb, vec3<f32>(regs[1].a),
            regs[2].rgb, vec3<f32>(regs[2].a), regs[3].rgb, vec3<f32>(regs[3].a),
            tex.rgb, vec3<f32>(tex.a), ras.rgb, vec3<f32>(ras.a),
            vec3<f32>(1.0), vec3<f32>(0.5), kc.rgb, vec3<f32>(0.0),
        );
        // Alpha inputs: APREV A0 A1 A2 TEXA RASA KONST ZERO
        var ain = array<f32, 8>(regs[0].a, regs[1].a, regs[2].a, regs[3].a, tex.a, ras.a, ka, 0.0);

        let ca = clamp(cin[(cenv >> 12u) & 15u], vec3<f32>(0.0), vec3<f32>(1.0));
        let cb = clamp(cin[(cenv >> 8u) & 15u], vec3<f32>(0.0), vec3<f32>(1.0));
        let cc = clamp(cin[(cenv >> 4u) & 15u], vec3<f32>(0.0), vec3<f32>(1.0));
        let cd = cin[cenv & 15u];
        var col: vec3<f32>;
        if (((cenv >> 16u) & 3u) == 3u) {
            let mode = (((cenv >> 20u) & 3u) << 1u) | ((cenv >> 18u) & 1u);
            if ((mode >> 1u) == 3u) {
                // RGB8: per component
                let eq = (mode & 1u) == 1u;
                var r = cd;
                for (var k = 0u; k < 3u; k = k + 1u) {
                    let hit = select(u8v(ca[k]) > u8v(cb[k]), u8v(ca[k]) == u8v(cb[k]), eq);
                    if (hit) { r[k] = r[k] + cc[k]; }
                }
                col = r;
            } else {
                col = cd + select(vec3<f32>(0.0), cc, cmp_scalar(ca, cb, mode));
            }
            col = clamp(col, vec3<f32>(0.0), vec3<f32>(1.0));
        } else {
            let lerp = ca * (vec3<f32>(1.0) - cc) + cb * cc;
            var r = select(cd + lerp, cd - lerp, ((cenv >> 18u) & 1u) == 1u);
            col = vec3<f32>(bias_scale(r.r, cenv), bias_scale(r.g, cenv), bias_scale(r.b, cenv));
        }

        let aa = clamp(ain[(aenv >> 13u) & 7u], 0.0, 1.0);
        let ab = clamp(ain[(aenv >> 10u) & 7u], 0.0, 1.0);
        let ac = clamp(ain[(aenv >> 7u) & 7u], 0.0, 1.0);
        let ad = ain[(aenv >> 4u) & 7u];
        var alpha: f32;
        if (((aenv >> 16u) & 3u) == 3u) {
            let mode = (((aenv >> 20u) & 3u) << 1u) | ((aenv >> 18u) & 1u);
            var hit: bool;
            if ((mode >> 1u) == 3u) {
                hit = select(u8v(aa) > u8v(ab), u8v(aa) == u8v(ab), (mode & 1u) == 1u);
            } else {
                hit = cmp_scalar(ca, cb, mode);
            }
            alpha = clamp(ad + select(0.0, ac, hit), 0.0, 1.0);
        } else {
            let lerp = aa * (1.0 - ac) + ab * ac;
            alpha = bias_scale(select(ad + lerp, ad - lerp, ((aenv >> 18u) & 1u) == 1u), aenv);
        }

        let dc = (cenv >> 22u) & 3u;
        let da = (aenv >> 22u) & 3u;
        regs[dc] = vec4<f32>(col, regs[dc].a);
        regs[da].a = alpha;
    }

    let res = clamp(regs[0], vec4<f32>(0.0), vec4<f32>(1.0));

    // Alpha compare (BP 0xF3).
    let acr = tev.params.y;
    let a8 = u8v(res.a);
    let t0 = alpha_test(a8, (acr >> 16u) & 7u, acr & 0xFFu);
    let t1 = alpha_test(a8, (acr >> 19u) & 7u, (acr >> 8u) & 0xFFu);
    let logic = (acr >> 22u) & 3u;
    var pass_test: bool;
    switch (logic) {
        case 0u: { pass_test = t0 && t1; }
        case 1u: { pass_test = t0 || t1; }
        case 2u: { pass_test = t0 != t1; }
        default: { pass_test = t0 == t1; }
    }
    if (!pass_test) {
        discard;
    }
    // Fog (same model as the hardware: eye depth from screen depth, then the selected curve).
    var rgb = res.rgb;
    let fogsel = u32(tev.fog_color.w);
    let fsel = fogsel & 7u;
    if (fsel >= 2u && fsel != 3u) { // 0 off, 1 and 3 reserved
        let zs = clamp(i.clip.z * 16777215.0, 0.0, 16777215.0);
        var ze: f32;
        if ((fogsel >> 3u) == 0u) {
            // ze = A * 2^24 / (B_mag - (Zs >> B_shift)), as Dolphin's pixel shader does.
            let shifted = floor(zs / exp2(tev.fog.w));
            ze = tev.fog.x * 16777216.0 / max(tev.fog.z - shifted, 1.0);
        } else {
            ze = tev.fog.x * zs / 16777216.0;
        }
        var f = clamp(ze - tev.fog.y, 0.0, 1.0);
        if (fsel == 4u) { f = 1.0 - exp2(-8.0 * f); }
        if (fsel == 5u) { f = 1.0 - exp2(-8.0 * f * f); }
        if (fsel == 6u) { f = exp2(-8.0 * (1.0 - f)); }
        if (fsel == 7u) { f = exp2(-8.0 * (1.0 - f) * (1.0 - f)); }
        rgb = mix(rgb, tev.fog_color.rgb, f);
    }
    // The EFB holds gamma-space values like the hardware; blending happens there too.
    return vec4<f32>(rgb, res.a);
}
