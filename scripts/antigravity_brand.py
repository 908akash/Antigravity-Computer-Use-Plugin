"""Official Antigravity 4-color brand mark + sleek split-tail navigation dart cursor mask."""

from __future__ import annotations

from functools import lru_cache
import io
import json
import os
from pathlib import Path
import struct
import zlib
import numpy as np
from PIL import Image, ImageDraw

# 192x192 (6x supersampled for 32x32) antialiased alpha mask of the sleek split-tail dart cursor,
# with top-left nose hotspot at (3, 3) in 32x32 coordinates.
DART_MASK_192_ZLIB_HEX = (
    "78daed5c796c544518ff66de70b682882b410eab316a2878811c111021065454e4344224120d8846024dd0204140620cfe81"
    "829ca20111f0081e91c30822a7912a0914c100825485082814215afa66e673dedb2ebcd2ddb7afbbbcdd79717e7f2c6d43da"
    "f9667edfef3be6003030303030303030303030c814047aae2e3fb677d6cdea6b4623377c8b4caf4407a73f185ae07e1f3103"
    "5e4129a4944299707052b1638115a5e1b7abe4c29d7fc939a2bd63741345a9082dc2ebc83101e17c59beb08ffa318d8a276c"
    "93022f42ba266c1ad95a2d42349c79337ac7ef2c822d112b5676711621023c9a72e9f8151c4f90df4e6def38b3ee8b70f339"
    "94b50c50ceac3eceaf7ca0b1fe8bb059724c02296cf5b9bfe43a27ace9aca813d1c6e4908eb29eddf07463adc35aab8a6404"
    "f22aeafed77ae81bd62cf850da981a6e58c3cd039b6aaaa88c3c891c7de12aeaefcb7a6aa9a8145a9df1215042511d91dd30"
    "ae584345b5e01db4312d5c95aa5ad183399ea0d32258a45792109674111c33f74c6bab57a140a051794003d42228a69d5e39"
    "a2a94e8a6ac1922004f22aeae1c9b7e9e30916f4960283c35554b16d6843478e74308190d274129a34accdeeea2a6afe4300"
    "4cad03813cb905ae1959a481335bd0357d044816d6d4c7b98f7a92bc2b2aa5dfd49140dedc62e7a4a23ccb1183a1198dff82"
    "a22eead7309fb90581167f67c2200f8ff68d6b9d474f20b04972cc18ae33577cfa10cd57eb85c15398c5f8138abaf3a58ef9"
    "096b040a8ea0c8ca80f822c8b5f75f910f4fb06081b4315bb89e50bea043ee3d81c1c06ce7ff62a150f9f9a8e6395654420a"
    "0f5e1e03e285c26f533ae4b66fc16002da8897c90265022f1dd528877244a1bd9f00894ce468ffacee399323020d0ef88d52"
    "d6d582782778cd90164edf82e482403352850089cbe76306f1c195a3930b6f875c247814daa4ca21041e6cf2e05ee4196418"
    "ee9ec8b6175ae7408e287c9e2a8710b802ae5d8519e6a8ce22ccb98785ddc163e4999403e4380e6002cf4ca1a4dbb7d8fd5c"
    "2cdcb046a16dca2454da621040b752149965a9d2e9e01d5f35c0ad35c2e291058b534eb090a76e27d0780e669ce6b972b475"
    "7cbbf014d522fd522ba8c05f9aabbffbd439b46536095ee5d26e6175f0085cf567ea2a86e3fa7a563d68f715669167c43b78"
    "535d390a83406ffb78a88def0263405fae429e6576f4d7b2c79a000d61fc9dfc34dec6194a3f08dcfb7346a1a0a6271c1a1e"
    "820184eef221879aba3e2a4c3368b602b3abd6dc046f721839c44c3f8917e26407b5488ab9634e65596eaa3a41de1902813a"
    "fa3283e34f3142d53241879d99eb50e277cd0a2386adf79d581b37baf18741c3c5754f492f89281bc320d0287f62d8b8149c"
    "6c5871e8d9caac38c4e5c761ccff0d95fe8d2c1b272a2b1d57879e3f66c121e5c10320f70452990c0e720d501f85cb944b67"
    "a6a0ea6fcc0ba78a79344d7895b2a2381e3cd54789ac3b87e24ddf5dc3218c3c8e40c1ef690c50d54c2c1e7b14871e3d5ac7"
    "9cda0d5ebfbe3fac3e8493865ab03c5d23cbc6b5acbaa265d076731d38246cf55fededa39a8693ffc44734386d7e662beeb2"
    "84b96c5ac060ec76dab1b4a438d472989046fb0318303e61808a06838f05e090bbddb4e7adbe24ec4a98c1f4b4c3519950df"
    "84018441d18e3442eaa6cd273febcf72d018b5a07bfaad0c212aba5e60308346f37c8a02d763f1eb21b19cb4519cedf803e9"
    "2b1481475a5f4880d5bfcf9f4fbe6871d26f999cc3d38016940470488edf1790c4788805bd0fd7e6505ce9cb97f7825ceecb"
    "5068196433ccc64fe9c5be2083d82778c949526741fefdb05761ae0f3e11d81a6433ac0aa7267c382ee7533cc1d8fd0562dd"
    "989b20f707cf582002c57b42cc53bbc1c3bfa113a0aa7953f64627c8cba62a81d88920bba952fed3d11b4619b45c973858f0"
    "c7dcbb1465687ef6532d782fd06698c0a3377aab700bc8c85267f04b065f0d793c5ec3c813c152028e3f34f51e545754215d"
    "86de171f7cfece131068732ed876bc8d6bc03b5012a753be0fc8f937b26a1a30176aecae104b83d37d16e91cb4d16c638947"
    "843401810687027639958af607ed4e165bb020685d25e4a99b806a37fe4e815b0b1c771552a21d83b605aecc6d1cab9d0b30"
    "783170612e64597da2d9025028ae92411924b1b3762e4c6175600249ecaeddf8190c0a3a7e210f3626fa39f03567021ee973"
    "3687b50b618a406b832d8090479b11fd6ef758d03758839fe308d0f16a0fa1654172088ebb09d5f1721583d941aa188e8f6b"
    "39fd6a500f04987f2ebf217a5e9124a4e1def406707c5843f1a926d0c4b43904c7edf5a9a6570b29dc9aae8a918277045d2f"
    "7712a8b72f0d8138ae067defa632989266334f9c6d4ff4bde0ec7722ab3af39f033a5f0da6b0c1af132a45e52d44e7fbe58c"
    "4cf253208e33b59e7e35ffd7fb24a1aa728f11bdaff75bb030750ec1712268fed408238fa4545055f6d6a39abf0fa1aa98d3"
    "a908c4718cae99839740a9ce840ab9a78012fdc7df4da63ad4fd20e8ffd00e81fac977533996311a8157762c5894944082f7"
    "8328bc736441d2dbf11cbf80683cd344c9a624499c107793688c9fc1d8da04e2383f22d3ef5c4caa55c548595944a2f2c014"
    "a97db999e34288cc2365b54f640971221699e95721a0d9c99a3904c769fa670e5e0ff8b2461523e4812b69845ee96330ba86"
    "03701c19a5e957046ae27d2045e091421aa9471219bce9a962847c0422f5422258c473a898e31688d82b9b0462172f2609d1"
    "9b446bfa9d24eed5440e61e30a88daf09dddd4ea7b2552c83b2237fdce01c2b27808a8c205d19b7e87408f29e24ba542c775"
    "dcab0bc2a0c92e7f0ef5000a104903867d77ecd09256111dbe81818181818181818181818181818181818181818181818181"
    "81818181818181818181818181818181c1ff14ff0107728841"
)


def _procedural_dart_mask_192() -> Image.Image:
    """Zero-failure procedural fallback matching the sleek split-tail navigation dart."""
    img = Image.new("L", (192, 192), 0)
    pts = [(16, 16), (168, 84), (102, 102), (84, 168)]
    d = ImageDraw.Draw(img)
    d.polygon(pts, fill=255)
    d.line(pts + [pts[0]], fill=255, width=10, joint="curve")
    return img


@lru_cache(maxsize=1)
def load_dart_cursor_mask_192() -> Image.Image:
    try:
        raw = zlib.decompress(bytes.fromhex(DART_MASK_192_ZLIB_HEX))
        if len(raw) == 192 * 192:
            return Image.frombytes("L", (192, 192), raw)
    except Exception:
        pass
    return _procedural_dart_mask_192()


def _find_asar_icon_bytes() -> bytes | None:
    candidates: list[Path] = []
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        prog = os.environ.get("ProgramFiles") or ""
        prog86 = os.environ.get("ProgramFiles(x86)") or ""
        candidates.append(Path(local_app) / "Programs" / "antigravity" / "resources" / "app.asar")
        if prog:
            candidates.append(Path(prog) / "Antigravity" / "resources" / "app.asar")
        if prog86:
            candidates.append(Path(prog86) / "Antigravity" / "resources" / "app.asar")
    else:
        candidates.extend(
            [
                Path("/Applications/Antigravity.app/Contents/Resources/app.asar"),
                Path.home() / "Applications" / "Antigravity.app" / "Contents" / "Resources" / "app.asar",
            ]
        )
    for asar in candidates:
        if not asar.is_file():
            continue
        try:
            with asar.open("rb") as fh:
                head = fh.read(16)
                if len(head) < 16:
                    continue
                _, header_size, _, json_len = struct.unpack("<IIII", head)
                if not (0 < header_size <= 32 * 1024 * 1024) or not (0 < json_len <= 16 * 1024 * 1024):
                    continue
                raw_json = fh.read(json_len)
                if len(raw_json) < json_len:
                    continue
                header_json = json.loads(raw_json.decode("utf-8"))
                data_base = 8 + header_size

                def _walk(node: dict) -> tuple[int, int] | None:
                    files = node.get("files")
                    if not isinstance(files, dict):
                        return None
                    if "icon.png" in files:
                        inf = files["icon.png"]
                        if isinstance(inf, dict) and "offset" in inf and "size" in inf:
                            return int(inf["offset"]), int(inf["size"])
                    for sub in files.values():
                        if isinstance(sub, dict):
                            found = _walk(sub)
                            if found is not None:
                                return found
                    return None

                loc = _walk(header_json)
                if loc is not None:
                    off, sz = loc
                    if off < 0 or not (0 < sz <= 8 * 1024 * 1024):
                        continue
                    fh.seek(data_base + off)
                    blob = fh.read(sz)
                    if len(blob) == sz:
                        return blob
        except Exception:
            continue
    return None


def _unmat_white_squircle(raw_png: bytes) -> Image.Image:
    raw_icon = Image.open(io.BytesIO(raw_png)).convert("RGBA")
    arr = np.asarray(raw_icon, dtype=np.float32)
    rgb = arr[..., :3]
    a_orig = arr[..., 3]
    dist_white = np.max(255.0 - rgb, axis=-1)
    fg_alpha = np.clip((dist_white - 3.0) / 120.0, 0.0, 1.0)
    safe_a = np.maximum(fg_alpha[..., None], 0.08)
    fg_rgb = np.clip(255.0 - (255.0 - rgb) / safe_a, 0.0, 255.0)
    final_a = np.minimum(a_orig, fg_alpha * 255.0)

    clean_arr = np.zeros_like(arr, dtype=np.uint8)
    clean_arr[..., :3] = np.round(fg_rgb).astype(np.uint8)
    clean_arr[..., 3] = np.round(final_a).astype(np.uint8)
    clean_img = Image.fromarray(clean_arr, mode="RGBA")
    bbox = clean_img.getbbox()
    cropped = clean_img.crop(bbox) if bbox else clean_img
    side = max(cropped.size)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(cropped, ((side - cropped.width) // 2, (side - cropped.height) // 2))
    return sq.resize((128, 128), Image.Resampling.LANCZOS)


def _procedural_fallback_mark() -> Image.Image:
    sz = 256
    mask = Image.new("L", (sz, sz), 0)
    pts = [(128, 20), (220, 224), (186, 232), (128, 134), (70, 232), (36, 224)]
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    yy = np.linspace(0.0, 1.0, sz, dtype=np.float32)[:, None]
    xx = np.linspace(0.0, 1.0, sz, dtype=np.float32)[None, :]
    arr = np.zeros((sz, sz, 4), dtype=np.uint8)
    arr[..., 0] = np.clip(250.0 * (1.0 - yy) + 50.0 * yy, 0, 255).astype(np.uint8)
    arr[..., 1] = np.clip(110.0 + 90.0 * (1.0 - xx) * yy, 0, 255).astype(np.uint8)
    arr[..., 2] = np.clip(60.0 * (1.0 - yy) + 255.0 * yy, 0, 255).astype(np.uint8)
    arr[..., 3] = np.asarray(mask, dtype=np.uint8)
    return Image.fromarray(arr, mode="RGBA").resize((128, 128), Image.Resampling.LANCZOS)


@lru_cache(maxsize=1)
def load_antigravity_mark() -> Image.Image:
    raw = _find_asar_icon_bytes()
    if raw:
        try:
            return _unmat_white_squircle(raw)
        except Exception:
            pass
    return _procedural_fallback_mark()
