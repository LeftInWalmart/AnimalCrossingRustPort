"""Copy every file listed in disc_files.csv out of the disc image into <outdir>/files/."""
import csv, sys
from pathlib import Path
from ciso_extract import open_disc

def main(iso, outdir):
    disc = open_disc(iso)
    out = Path(outdir)
    for row in csv.DictReader(open(out / "disc_files.csv")):
        dst = out / "files" / row["path"].lstrip("/")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(disc.read(int(row["disc_offset"], 16), int(row["size"])))
        print(row["path"], row["size"])

if __name__ == "__main__":
    main(*sys.argv[1:3])
