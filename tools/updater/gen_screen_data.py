"""
Builds screen_data.json for the Controller tab's screen from the controller firmware's own sources, so the emulated
screen uses the same pixel font, window titles and texts as the real controller:
  - main/src/Fonts/lv_font_butter.c                (the 5 px font: one bit per pixel, glyphs packed bit by bit)
  - main/src/Components/HomeWindows/*.h / *.cpp     (window titles and their fixed sentences)
  - components/ButterBot-Common/src/Phrases.cpp     (the texts shown for facts, jokes, rambles, pokes...)
The controller sources live in the controller repository, so this isn't run by build_exe.ps1; run it by hand when
the controller's windows change and commit the result:
    python gen_screen_data.py <path to ButterBotCtrl-Firmware-Public>
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent


def parse_font(src: str) -> dict:
    bitmap = [int(x, 16) for x in re.findall(r"0x([0-9a-f]+)",
                                            re.search(r"glyph_bitmap\[\] = \{(.*?)\n\};", src, re.S).group(1))]
    dscs = [dict((k, int(v)) for k, v in re.findall(r"\.(\w+) = (-?\d+)", m))
            for m in re.findall(r"\{(\.bitmap_index[^}]*)\}", re.search(r"glyph_dsc\[\] = \{(.*?)\n\};", src, re.S).group(1))]
    # character -> glyph id, from the cmaps
    chars: dict[int, int] = {}
    sparse0 = [int(x, 16) for x in re.findall(r"0x([0-9a-f]+)",
                                             re.search(r"unicode_list_0\[\] = \{(.*?)\};", src, re.S).group(1))]
    ofs2 = [int(x) for x in re.findall(r"\d+", re.search(r"glyph_id_ofs_list_2\[\] = \{(.*?)\};", src, re.S).group(1))]
    for cm in re.finditer(r"\.range_start = (\d+), \.range_length = (\d+), \.glyph_id_start = (\d+),\s*"
                          r"\.unicode_list = (\w+), \.glyph_id_ofs_list = (\w+),.*?\.type = (\w+)", src, re.S):
        start, length, gid, ulist, olist, typ = cm.groups()
        start, length, gid = int(start), int(length), int(gid)
        if typ.endswith("SPARSE_TINY"):
            for i, off in enumerate(sparse0):
                chars[start + off] = gid + i
        elif typ.endswith("FORMAT0_TINY"):
            for i in range(length):
                chars[start + i] = gid + i
        elif typ.endswith("FORMAT0_FULL"):
            for i in range(length):
                if ofs2[i] or i == 0:
                    chars[start + i] = gid + ofs2[i]
    glyphs = {}
    for code, gid in chars.items():
        d = dscs[gid]
        w, h = d["box_w"], d["box_h"]
        bits = []
        start = d["bitmap_index"] * 8
        for i in range(w * h):
            b = start + i
            bits.append((bitmap[b // 8] >> (7 - b % 8)) & 1 if b // 8 < len(bitmap) else 0)
        rows = ["".join(str(bits[r * w + c]) for c in range(w)) for r in range(h)]
        glyphs[chr(code)] = {"adv": round(d["adv_w"] / 16), "w": w, "h": h, "x": d["ofs_x"], "y": d["ofs_y"],
                             "rows": rows}
    lh = int(re.search(r"\.line_height = (\d+)", src).group(1))
    base = int(re.search(r"\.base_line = (\d+)", src).group(1))
    return {"line_height": lh, "base_line": base, "glyphs": glyphs}


def unescape(s: str) -> str:
    return bytes(s, "utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")


def parse_phrases(src: str) -> dict:
    out = {}
    for m in re.finditer(r"static constexpr std::array (\w+) = std::to_array<Phrases::PhraseOutput>\(\s*\{(.*?)\}\s*\);",
                         src, re.S):
        name, body = m.groups()
        texts = []
        for e in re.finditer(r'\{\s*"((?:[^"\\]|\\.)*)"\s*,[^,}]*,[^,}]*(?:,\s*"((?:[^"\\]|\\.)*)")?\s*\}', body):
            pronounced, shown = e.group(1), e.group(2)
            texts.append(unescape(shown if shown else pronounced))
        out[name] = texts
    return out


def parse_windows(root: Path) -> dict:
    out = {}
    for h in sorted(root.glob("*Window.h")):
        name = h.stem
        text = h.read_text(encoding="utf-8")
        cpp = h.with_suffix(".cpp")
        both = text + (cpp.read_text(encoding="utf-8") if cpp.exists() else "")
        title = re.search(r'TITLE = "([^"]*)"', text)
        sentences = dict(re.findall(r'(\w*Sentence) = "([^"]*)"', both))
        out[name] = {"title": title.group(1) if title else "", "sentences": sentences}
    return out


def main():
    ctrl = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parents[2] / "ctrl"
    font = parse_font((ctrl / "main/src/Fonts/lv_font_butter.c").read_text(encoding="utf-8"))
    phrases = parse_phrases((ctrl / "components/ButterBot-Common/src/Phrases.cpp").read_text(encoding="utf-8"))
    windows = parse_windows(ctrl / "main/src/Components/HomeWindows")
    listen = dict(re.findall(r'ListenData::Phase::(\w+), "([^"]*)"',
                             (ctrl / "main/src/Components/HomeWindows/ListenWindow.cpp").read_text(encoding="utf-8")))
    data = {"font": font, "phrases": phrases, "windows": windows, "listen": listen}
    out = HERE / "screen_data.json"
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{out.name}: {len(font['glyphs'])} glyphs, {len(phrases)} phrase lists, {len(windows)} windows")


if __name__ == "__main__":
    main()
