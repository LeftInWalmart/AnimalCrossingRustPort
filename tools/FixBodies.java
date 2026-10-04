// Make every function body exactly its linker-map range (symbols.csv: addr + size).
// Auto-analysis cuts a function short after calls it believes never return (asserts, OSPanic
// wrappers, ...); CreateGapFunctions then turned each cut-off tail into a separate function, and
// a loop crossing such a cut would recurse in the recompiled code. The shipped maps give the true
// extent of every function, so:
//   1. functions that start strictly inside a map function's range are removed;
//   2. map functions are shrunk to their range (so neighbours can grow into theirs);
//   3. defined data inside the range is cleared, every undefined word is disassembled
//      (restricted to the range), and the body is set to the full range.
// Usage: ghidra script run FixBodies.java -- <symbols.csv>
//@category Analysis
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.SourceType;

import java.nio.file.*;
import java.util.*;

public class FixBodies extends GhidraScript {
    @Override
    protected void run() throws Exception {
        AddressSpace ram = currentProgram.getAddressFactory().getDefaultAddressSpace();
        FunctionManager fm = currentProgram.getFunctionManager();
        Listing listing = currentProgram.getListing();
        TreeMap<Long, Long> ranges = new TreeMap<>();
        Map<Long, String> names = new HashMap<>();
        List<String> lines = Files.readAllLines(Paths.get(getScriptArgs()[0]));
        for (String line : lines.subList(1, lines.size())) {
            String[] f = line.split(",");
            if (f.length < 4 || !(f[3].equals(".text") || f[3].equals(".init"))) continue;
            long a = Long.parseLong(f[0].substring(2), 16), size = Long.parseLong(f[2]);
            if (size <= 0) continue;
            Address addr = ram.getAddress(a);
            MemoryBlock b = currentProgram.getMemory().getBlock(addr);
            if (b == null || !b.isExecute()) continue;
            ranges.putIfAbsent(a, size);
            names.putIfAbsent(a, f[1]);
        }
        int removed = 0, shrunk = 0, disasm = 0, set = 0, failed = 0;
        // 1 + 2
        for (Map.Entry<Long, Long> e : ranges.entrySet()) {
            if (monitor.isCancelled()) return;
            Address a = ram.getAddress(e.getKey());
            AddressSet range = new AddressSet(a, a.add(e.getValue() - 1));
            List<Function> overl = new ArrayList<>();
            fm.getFunctionsOverlapping(range).forEachRemaining(overl::add);
            for (Function g : overl) {
                long ge = g.getEntryPoint().getOffset();
                if (ge == e.getKey()) continue;
                if (ge > e.getKey() && ge < e.getKey() + e.getValue()) {
                    fm.removeFunction(g.getEntryPoint());
                    removed++;
                } else {
                    // A neighbour reaching into this range: cut it back to what lies outside.
                    AddressSet nb = new AddressSet(g.getBody());
                    nb.delete(range);
                    if (!nb.isEmpty() && nb.contains(g.getEntryPoint())) {
                        try { g.setBody(nb); shrunk++; } catch (Exception ex) { failed++; }
                    }
                }
            }
            Function f = fm.getFunctionAt(a);
            if (f != null) {
                AddressSet nb = new AddressSet(f.getBody());
                nb = nb.intersect(range);
                if (!nb.contains(a)) nb.add(a);
                try { f.setBody(nb); } catch (Exception ex) { failed++; }
            }
        }
        // 3
        for (Map.Entry<Long, Long> e : ranges.entrySet()) {
            if (monitor.isCancelled()) return;
            Address a = ram.getAddress(e.getKey());
            Address end = a.add(e.getValue() - 1);
            AddressSet range = new AddressSet(a, end);
            for (Data d : listing.getDefinedData(range, true)) {
                listing.clearCodeUnits(d.getAddress(), d.getMaxAddress(), false);
            }
            for (Address p = a; p.compareTo(end) <= 0; p = p.add(4)) {
                if (listing.getInstructionContaining(p) == null) {
                    new DisassembleCommand(p, range, true).applyTo(currentProgram, monitor);
                    disasm++;
                }
            }
            Function f = fm.getFunctionAt(a);
            try {
                if (f == null) {
                    fm.createFunction(names.get(e.getKey()), a, range, SourceType.IMPORTED);
                } else {
                    f.setBody(range);
                }
                set++;
            } catch (Exception ex) {
                failed++;
                if (failed < 30) println("body " + a + " " + names.get(e.getKey()) + ": " + ex.getMessage());
            }
        }
        println("done: " + set + " bodies set, " + removed + " inner functions removed, " + shrunk
            + " neighbours shrunk, " + disasm + " disassembly starts, " + failed + " failures; total "
            + fm.getFunctionCount());
    }
}
