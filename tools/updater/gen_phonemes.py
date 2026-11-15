"""
Builds phonemes.txt.gz (word -> MultiNet phonemes) from the CMU Pronouncing Dictionary, for custom voice commands.

    python gen_phonemes.py <path to cmudict.dict>

cmudict.dict: https://github.com/cmusphinx/cmudict (BSD-style licence, see CMUDICT_LICENSE next to this file).
The ARPAbet -> MultiNet alphabet is the one in esp-sr's tool/multinet_g2p.py.
"""
import gzip
import sys
from pathlib import Path

ALPHABET = {"AE1": "a", "N": "N", "OW1": "b", "V": "V", "AH0": "c", "L": "L", "F": "F", "EY1": "d", "S": "S",
            "B": "B", "R": "R", "AO1": "e", "D": "D", "AH1": "c", "EH1": "f", "OW0": "b", "IH0": "g", "G": "G",
            "HH": "h", "K": "K", "IH1": "g", "W": "W", "AY1": "i", "T": "T", "M": "M", "Z": "Z", "DH": "j",
            "ER0": "k", "P": "P", "NG": "l", "IY1": "m", "AA1": "n", "Y": "Y", "UW1": "o", "IY0": "m", "EH2": "f",
            "CH": "p", "AE0": "a", "JH": "q", "ZH": "r", "AA2": "n", "SH": "s", "AW1": "t", "OY1": "u", "AW2": "t",
            "IH2": "g", "AE2": "a", "EY2": "d", "ER1": "k", "TH": "v", "UH1": "w", "UW2": "o", "OW2": "b", "AY2": "i",
            "UW0": "o", "AH2": "c", "EH0": "f", "AW0": "t", "AO2": "e", "AO0": "e", "UH0": "w", "UH2": "w", "AA0": "n",
            "AY0": "i", "IY2": "m", "EY0": "d", "ER2": "k", "OY2": "u", "OY0": "u"}


def arpabet_to_mn(phones: list[str]) -> str:
    return "".join(ALPHABET.get(p, "") for p in phones)


def main(src: str):
    out: dict[str, str] = {}
    for line in Path(src).read_text(encoding="ascii", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        word, *phones = line.split()
        if "(" in word:  # alternative pronunciations: keep the first
            continue
        mn = arpabet_to_mn(phones)
        if mn and word.replace("'", "").isalpha():
            out[word.lower()] = mn
    dest = Path(__file__).with_name("phonemes.txt.gz")
    with gzip.open(dest, "wt", encoding="ascii", compresslevel=9) as f:
        for w in sorted(out):
            f.write(f"{w}\t{out[w]}\n")
    print(f"{len(out)} words -> {dest} ({dest.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1])
