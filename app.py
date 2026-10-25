#!/usr/bin/env python3
"""
Simple local media server: browse, play, move, delete JPEG and MP4 files
from a remote browser.

Run:
    python3 app.py [--dir /path/to/media] [--port 8000] [--host 0.0.0.0]

Then open http://<your-linux-ip>:8000 in a remote browser.
"""
import argparse
import os
import shutil
import subprocess
import tempfile
from collections import deque
from pathlib import Path
from urllib.parse import quote

from flask import Flask, request, redirect, url_for, send_file, abort, render_template_string

ALLOWED_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
ALLOWED_VIDEO = {".mp4", ".webm", ".mov"}
ALLOWED_ALL = ALLOWED_IMAGE | ALLOWED_VIDEO

app = Flask(__name__)
MEDIA_ROOT = Path("media").resolve()

# Most recently opened folders (relative paths, "" = media root), newest first.
RECENT_FOLDERS = deque(maxlen=8)

def record_recent(rel: str):
    rel = rel or ""
    try:
        RECENT_FOLDERS.remove(rel)
    except ValueError:
        pass
    RECENT_FOLDERS.appendleft(rel)

def recent_folders():
    """Recent folders that still exist, newest first."""
    root = MEDIA_ROOT.resolve()
    out = []
    for r in RECENT_FOLDERS:
        p = (root / r).resolve() if r else root
        if p == root or (root in p.parents and p.is_dir()):
            out.append(r)
    return out

def unique_dest(dst: Path) -> Path:
    """If dst exists, append a decimal number: photo.jpg -> photo1.jpg -> photo2.jpg ..."""
    if not dst.exists():
        return dst
    stem, suffix = dst.stem, dst.suffix
    i = 1
    while True:
        candidate = dst.parent / f"{stem}{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1

def safe_path(rel: str) -> Path:
    """Resolve a user-supplied relative path, blocking traversal outside MEDIA_ROOT."""
    p = (MEDIA_ROOT / rel).resolve() if rel else MEDIA_ROOT.resolve()
    if p != MEDIA_ROOT.resolve() and MEDIA_ROOT.resolve() not in p.parents:
        abort(400, "Invalid path")
    return p

def rel_of(p: Path) -> str:
    return str(p.relative_to(MEDIA_ROOT.resolve()))

def file_kind(p: Path) -> str:
    ext = p.suffix.lower()
    if ext in ALLOWED_IMAGE:
        return "image"
    if ext in ALLOWED_VIDEO:
        return "video"
    return "other"

THUMB_SUFFIX = "_tb"

def thumb_path(p: Path) -> Path:
    """Companion thumbnail path: photo.jpg -> photo_tb.jpg, movie.mp4 -> movie_tb.jpg"""
    return p.parent / f"{p.stem}{THUMB_SUFFIX}.jpg"

def is_thumb_file(p: Path) -> bool:
    """True for companion thumbnails themselves (never listed as files)."""
    return p.suffix.lower() == ".jpg" and p.stem.endswith(THUMB_SUFFIX)

def _make_thumb(src: Path, dst: Path, video: bool):
    """Create dst as a 256x256-max JPEG of src, using ffmpeg for both images
    (plain scale) and videos (frame grab at ~0.5s, scaled)."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not installed")
    args = ["ffmpeg", "-y", "-v", "error"]
    if video:
        args += ["-ss", "0.5"]
    args += ["-i", str(src)]
    if video:
        args += ["-vframes", "1"]
    args += ["-vf", "scale=256:256:force_original_aspect_ratio=decrease",
             str(dst)]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError("ffmpeg thumbnail failed")

def ensure_thumb(p: Path):
    """Create the 256x256 _tb.jpg thumbnail for an image/video if missing,
    using ffmpeg. Never raises; returns the thumbnail path, or None when it
    can't be made (unsupported type, corrupt file, or ffmpeg missing)."""
    try:
        if is_thumb_file(p) or file_kind(p) not in ("image", "video"):
            return None
        tb = thumb_path(p)
        if tb.exists():
            return tb
        _make_thumb(p, tb, video=(file_kind(p) == "video"))
        return tb if tb.exists() else None
    except Exception:
        return None

def delete_thumb(p: Path):
    """Remove the companion thumbnail of p, if any. Never raises."""
    try:
        if not is_thumb_file(p):
            thumb_path(p).unlink(missing_ok=True)
    except Exception:
        pass

BROWSE_HTML = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Media Server</title>
<style>
body{font-family:system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;background:#111;color:#eee}
a{color:#7ab8ff}.row{display:flex;align-items:center;gap:.6rem;padding:.45rem .2rem;border-bottom:1px solid #2a2a2a}
.row:hover{background:#1a1a1a}.name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{font-size:.75rem;background:#2a2a2a;border-radius:6px;padding:.15rem .5rem;color:#bbb;white-space:nowrap}
form.inline{display:inline}button{background:#2a2a2a;color:#eee;border:1px solid #444;border-radius:8px;padding:.35rem .7rem;cursor:pointer}
button.danger{background:#5a1f1f;border-color:#a33}button.primary{background:#2a6a2a;border-color:#3a8a3a;color:#fff}
input[type=text]{background:#1c1c1c;color:#eee;border:1px solid #444;border-radius:8px;padding:.4rem .6rem;width:100%}
.crumb{margin-bottom:1rem;color:#aaa}
.thumb{width:96px;height:64px;object-fit:cover;border-radius:8px;background:#000;flex-shrink:0}
.toolbar{display:flex;gap:.6rem;align-items:center;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:.6rem .8rem;margin-bottom:1rem;position:sticky;top:0}
.toolbar label{color:#bbb;font-size:.9rem}
.uploadlabel{display:inline-block;background:#2a2a2a;color:#eee;border:1px solid #444;border-radius:8px;padding:.35rem .7rem;cursor:pointer}
.uploadlabel:hover{background:#333}
input[type=checkbox]{width:18px;height:18px;accent-color:#7ab8ff}
</style></head><body>
<h2>📁 Media Server</h2>
<div class="crumb">{{ crumbs|safe }}</div>

{% if parent is not none %}
<div class="row"><div class="name"><a href="/?path={{ parent|urlencode }}">⬆️ .. (up)</a></div></div>
{% endif %}
{% for d in dirs %}
<div class="row"><div class="name">📁 <a href="/?path={{ d.rel|urlencode }}">{{ d.name }}</a></div>
<span class="badge">folder</span>
<form class="inline" method="post" action="/delete" onsubmit="return confirm('Delete folder {{ d.name }} and everything inside?')">
<input type="hidden" name="path" value="{{ d.rel }}"><button class="danger">Delete</button></form>
<a href="/move?path={{ d.rel|urlencode }}"><button type="button">Move</button></a>
</div>
{% endfor %}

<form id="bulkform" method="post">
<input type="hidden" name="cur_path" value="{{ rel }}">
<div class="toolbar">
<input type="checkbox" id="selectall" title="Select all">
<label for="selectall">Select all</label>
<button type="button" id="s4btn" title="Select 4 more files starting from the last selected one">S4</button>
<span style="flex:1"></span>
<label for="filepicker" class="uploadlabel" title="Upload files to this folder">⬆️ Upload</label>
<input type="file" id="filepicker" name="files" multiple hidden form="uploadform">
<button type="submit" formaction="/bulk_delete" class="danger"
  onclick="return confirm('Delete selected files?') && requireSelected()">🗑️ Delete selected</button>
<button type="submit" formaction="/bulk_move" formmethod="get" class="primary"
  onclick="return requireSelected()">📦 Move selected</button>
<button type="submit" formaction="/bulk_merge" formmethod="get" class="primary"
  onclick="return requireSelected()">🎬 Merge videos</button>
</div>

{% for f in files %}
<div class="row">
<input type="checkbox" name="selected" value="{{ f.rel }}">
{% if f.thumb_rel %}
<a href="/view?path={{ f.rel|urlencode }}" target="_blank" rel="noopener"><img class="thumb" src="/media/{{ f.thumb_rel_quoted }}" loading="lazy" alt=""></a>
{% endif %}
<div class="name">{% if f.kind=='image' %}🖼️{% elif f.kind=='video' %}🎬{% else %}📄{% endif %}
<a href="/view?path={{ f.rel|urlencode }}" target="_blank" rel="noopener">{{ f.name }}</a><br><span class="badge">{{ f.kind }} · {{ f.size }}</span></div>
<a href="/view?path={{ f.rel|urlencode }}" target="_blank" rel="noopener"><button type="button">Play / View</button></a>
</div>
{% endfor %}
</form>

<form id="uploadform" action="/upload" method="post" enctype="multipart/form-data">
<input type="hidden" name="cur_path" value="{{ rel }}">
</form>

{% if not dirs and not files %}<p style="color:#888">Empty folder.</p>{% endif %}
<hr style="border-color:#2a2a2a;margin-top:2rem">
<p style="color:#666;font-size:.85rem">Serving: {{ root }} — tick checkboxes, then use the buttons at the top to move or delete multiple files.</p>
<script>
const all = document.getElementById('selectall');
all.addEventListener('change', () => {
  document.querySelectorAll('input[name=selected]').forEach(c => c.checked = all.checked);
});
function requireSelected(){
  const n = document.querySelectorAll('input[name=selected]:checked').length;
  if(!n){ alert('Select at least one file first.'); return false; }
  return true;
}
document.getElementById('s4btn').addEventListener('click', () => {  const boxes = Array.from(document.querySelectorAll('input[name=selected]'));
  let last = -1;
  boxes.forEach((c, i) => { if (c.checked) last = i; });
  let added = 0;
  for (let i = last + 1; i < boxes.length && added < 4; i++) {
    if (!boxes[i].checked) { boxes[i].checked = true; added++; }
  }
});
document.getElementById('filepicker').addEventListener('change', e => {
  if (e.target.files.length) e.target.form.submit();
});
</script>
</body></html>
"""

BULK_MOVE_HTML = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Move selected</title>
<style>body{font-family:system-ui,sans-serif;max-width:600px;margin:2rem auto;padding:0 1rem;background:#111;color:#eee}
input{background:#1c1c1c;color:#eee;border:1px solid #444;border-radius:8px;padding:.5rem .7rem;width:100%;margin:.4rem 0}
button{background:#2a6a2a;color:#fff;border:none;border-radius:8px;padding:.55rem 1.1rem;cursor:pointer}a{color:#7ab8ff}
li{color:#bbb;font-size:.9rem}</style></head><body>
<p><a href="/?path={{ cur|urlencode }}">← back</a></p>
<h3>Move {{ count }} file(s)</h3>
<ul>{% for f in files %}<li>{{ f }}</li>{% endfor %}</ul>
<form method="post" action="/bulk_move">
{% for f in files %}<input type="hidden" name="selected" value="{{ f }}">{% endfor %}
<input type="hidden" name="cur" value="{{ cur }}">
<label>Destination folder (relative to media root, e.g. <code>vacation/2026</code> — blank = current folder):</label>
<input type="text" name="dest" list="recentfolders" placeholder="folder/name" value="">
<datalist id="recentfolders">
{% for f in recent %}<option value="{{ f }}">{% endfor %}
</datalist>
<button>Move all here</button>
</form></body></html>
"""

MERGE_HTML = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Merge videos</title>
<style>body{font-family:system-ui,sans-serif;max-width:600px;margin:2rem auto;padding:0 1rem;background:#111;color:#eee}
input{background:#1c1c1c;color:#eee;border:1px solid #444;border-radius:8px;padding:.5rem .7rem;width:100%;margin:.4rem 0}
button{background:#2a6a2a;color:#fff;border:none;border-radius:8px;padding:.55rem 1.1rem;cursor:pointer}a{color:#7ab8ff}
li{color:#bbb;font-size:.9rem}.note{color:#888;font-size:.9rem}</style></head><body>
<p><a href="/?path={{ cur|urlencode }}">← back</a></p>
<h3>Merge {{ count }} clip(s) into one video</h3>
<p class="note">Clips are joined in the order listed below (same as the folder listing). Non-video files in your selection are skipped. Requires ffmpeg on the server.</p>
<ol>{% for f in files %}<li>{{ f }}</li>{% endfor %}</ol>
<form method="post" action="/bulk_merge">
{% for f in files %}<input type="hidden" name="selected" value="{{ f }}">{% endfor %}
<input type="hidden" name="cur_path" value="{{ cur }}">
<label>Output file name:</label>
<input type="text" name="outname" value="merged.mp4" required>
<button>Merge</button>
</form></body></html>
"""

VIEW_HTML = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ name }}</title>
<style>body{font-family:system-ui,sans-serif;background:#111;color:#eee;text-align:center;padding:1rem}
img,video{max-width:95vw;max-height:80vh;border-radius:12px;background:#000}
a{color:#7ab8ff} .bar{margin:1rem;display:flex;gap:.6rem;justify-content:center;align-items:center;flex-wrap:wrap}
button{background:#2a2a2a;color:#eee;border:1px solid #444;border-radius:8px;padding:.45rem .9rem;cursor:pointer}
button.danger{background:#5a1f1f;border-color:#a33}
button:disabled{opacity:.35;cursor:default}
.pos{color:#888;font-size:.9rem;min-width:4em}</style></head><body>
<p><a href="/?path={{ parent|urlencode }}">← back</a></p>
<h3>{{ name }}</h3>
<div class="bar">
{% if prev %}<a href="/view?path={{ prev|urlencode }}"><button>◀ Prev</button></a>
{% else %}<button disabled>◀ Prev</button>{% endif %}
<span class="pos">{{ pos }} / {{ total }}</span>
{% if next %}<a href="/view?path={{ next|urlencode }}"><button>Next ▶</button></a>
{% else %}<button disabled>Next ▶</button>{% endif %}
</div>
{% if kind == 'image' %}
<img src="/media/{{ rel_quoted }}" alt="{{ name }}">
{% elif kind == 'video' %}
<video src="/media/{{ rel_quoted }}" controls autoplay loop playsinline></video>
{% else %}
<p>Cannot preview this file type. <a href="/media/{{ rel_quoted }}" download>Download</a></p>
{% endif %}
<div class="bar">
<a href="/media/{{ rel_quoted }}" download><button>Download</button></a>
<a href="/move?path={{ rel|urlencode }}"><button>Move / Rename</button></a>
<form method="post" action="/delete" onsubmit="return confirm('Delete {{ name }}?')" style="display:inline">
<input type="hidden" name="path" value="{{ rel }}"><button class="danger">Delete</button></form>
</div>
<script>
document.addEventListener('keydown', e => {
  {% if prev %}if(e.key==='ArrowLeft') location.href='/view?path={{ prev|urlencode }}';{% endif %}
  {% if next %}if(e.key==='ArrowRight') location.href='/view?path={{ next|urlencode }}';{% endif %}
});
</script>
</body></html>
"""

MOVE_HTML = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Move {{ name }}</title>
<style>body{font-family:system-ui,sans-serif;max-width:600px;margin:2rem auto;padding:0 1rem;background:#111;color:#eee}
input{background:#1c1c1c;color:#eee;border:1px solid #444;border-radius:8px;padding:.5rem .7rem;width:100%;margin:.4rem 0}
button{background:#2a6a2a;color:#fff;border:none;border-radius:8px;padding:.55rem 1.1rem;cursor:pointer}a{color:#7ab8ff}</style>
</head><body>
<p><a href="/?path={{ parent|urlencode }}">← back</a></p>
<h3>Move / Rename</h3><p style="color:#aaa">{{ rel }}</p>
<form method="post" action="/move">
<input type="hidden" name="src" value="{{ rel }}">
<label>New location (relative to media root, e.g. <code>vacation/newname{{ '' if is_dir else '.mp4' }}</code>):</label>
<input type="text" name="dst" value="{{ rel }}" required>
<label style="margin-top:.6rem;display:block">Or pick a recent folder (keeps the {{ 'folder' if is_dir else 'file' }} name):</label>
<select id="recentpick" style="background:#1c1c1c;color:#eee;border:1px solid #444;border-radius:8px;padding:.5rem .7rem;width:100%">
<option value="">— select a recent folder —</option>
{% for f in recent %}<option value="{{ f }}">{{ f if f else '(media root)' }}</option>{% endfor %}
</select>
<div style="margin-top:.8rem"><button>Move</button></div>
</form>
<script>
document.getElementById('recentpick').addEventListener('change', function(e){
  var folder = e.target.value;
  var dst = document.querySelector('input[name=dst]');
  var cur = dst.value.replace(/\\/+$/,'');
  var base = cur.slice(cur.lastIndexOf('/')+1);
  dst.value = folder ? folder + '/' + base : base;
});
</script></body></html>
"""

def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"

@app.route("/")
def browse():
    rel = request.args.get("path", "")
    cur = safe_path(rel)
    if not cur.exists():
        abort(404)
    if cur.is_file():
        return redirect(url_for("view", path=rel))
    record_recent(rel)
    dirs, files = [], []
    for child in sorted(cur.iterdir(), key=lambda p: p.name.lower()):
        if is_thumb_file(child):
            continue  # companion thumbnails are not listed as files
        r = rel_of(child)
        if child.is_dir():
            dirs.append({"name": child.name, "rel": r})
        elif child.is_file():
            nbytes = child.stat().st_size
            tb = ensure_thumb(child)  # create the _tb.jpg if missing
            files.append({"name": child.name, "rel": r, "rel_quoted": quote(r),
                          "kind": file_kind(child),
                          "size": human_size(nbytes),
                          "thumb_rel": rel_of(tb) if tb else None,
                          "thumb_rel_quoted": quote(rel_of(tb)) if tb else None})
    # breadcrumbs
    parts = Path(rel).parts if rel else ()
    crumbs = '<a href="/?path=">root</a>'
    acc = Path()
    for part in parts:
        acc = acc / part
        crumbs += f' / <a href="/?path={quote(str(acc))}">{part}</a>'
    parent = str(Path(rel).parent) if rel and str(Path(rel).parent) != "." else ("" if not rel else "")
    # parent="" means root; parent=None hides the up row on root
    parent_val = None if not rel else parent
    return render_template_string(BROWSE_HTML, dirs=dirs, files=files,
                                  crumbs=crumbs, parent=parent_val, root=str(MEDIA_ROOT),
                                  rel=rel)

@app.route("/view")
def view():
    rel = request.args.get("path", "")
    p = safe_path(rel)
    if not p.is_file():
        abort(404)
    parent_rel = str(Path(rel).parent) if str(Path(rel).parent) != "." else ""
    # prev/next among media files in the same folder, sorted by name
    siblings = []
    parent_dir = p.parent
    if parent_dir.is_dir():
        for child in sorted(parent_dir.iterdir(), key=lambda x: x.name.lower()):
            if child.is_file() and not is_thumb_file(child):
                siblings.append(rel_of(child))
    prev_rel = next_rel = None
    pos = total = 0
    if rel in siblings:
        idx = siblings.index(rel)
        total = len(siblings)
        pos = idx + 1
        if idx > 0:
            prev_rel = siblings[idx - 1]
        if idx < len(siblings) - 1:
            next_rel = siblings[idx + 1]
    return render_template_string(VIEW_HTML, name=p.name, kind=file_kind(p),
                                  rel=rel, rel_quoted=quote(rel),
                                  parent=parent_rel,
                                  prev=prev_rel, next=next_rel, pos=pos, total=total)

@app.route("/media/<path:rel>")
def media(rel):
    p = safe_path(rel)
    if not p.is_file():
        abort(404)
    return send_file(p)

@app.route("/delete", methods=["POST"])
def delete():
    rel = request.form.get("path", "")
    p = safe_path(rel)
    if not p.exists():
        abort(404)
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    except Exception as e:
        abort(500, str(e))
    delete_thumb(p)
    parent = str(Path(rel).parent) if str(Path(rel).parent) != "." else ""
    return redirect(url_for("browse", path=parent))

@app.route("/move", methods=["GET", "POST"])
def move():
    if request.method == "GET":
        rel = request.args.get("path", "")
        p = safe_path(rel)
        if not p.exists():
            abort(404)
        parent = str(Path(rel).parent) if str(Path(rel).parent) != "." else ""
        return render_template_string(MOVE_HTML, name=p.name, rel=rel, parent=parent,
                                      recent=recent_folders(), is_dir=p.is_dir())
    src_rel = request.form.get("src", "")
    dst_rel = request.form.get("dst", "")
    src = safe_path(src_rel)
    dst = safe_path(dst_rel)
    if not src.exists():
        abort(404, "Source not found")
    src_is_dir = src.is_dir()
    if dst.is_dir():
        dst = dst / src.name  # picked a folder: keep the file name
    if src_is_dir and (dst == src or src in dst.parents):
        abort(400, "Cannot move a folder into itself")
    dst = unique_dest(dst)  # auto-rename on conflict: photo.jpg -> photo1.jpg
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not src_is_dir:
        delete_thumb(src)  # drop the thumbnail at the old location
    shutil.move(str(src), str(dst))
    if not src_is_dir:
        ensure_thumb(dst)  # and recreate it next to the moved file
    parent = str(Path(dst_rel).parent) if str(Path(dst_rel).parent) != "." else ""
    return redirect(url_for("browse", path=parent))

@app.route("/bulk_delete", methods=["POST"])
def bulk_delete():
    rels = request.form.getlist("selected")
    if not rels:
        abort(400, "No files selected")
    cur_path = request.form.get("cur_path", "")
    for rel in rels:
        p = safe_path(rel)
        if p.is_file():
            p.unlink(missing_ok=True)
            delete_thumb(p)
        # skip dirs silently for bulk file ops; single delete handles folders
    return redirect(url_for("browse", path=cur_path))


@app.route("/bulk_move", methods=["GET", "POST"])
def bulk_move():
    if request.method == "GET":
        rels = request.args.getlist("selected")
        if not rels:
            abort(400, "No files selected")
        cur = request.args.get("cur_path", "") or (
            str(Path(rels[0]).parent) if str(Path(rels[0]).parent) != "." else "")
        return render_template_string(BULK_MOVE_HTML, files=rels, count=len(rels), cur=cur,
                                      recent=recent_folders())
    rels = request.form.getlist("selected")
    dest_rel = request.form.get("dest", "").strip()
    cur = request.form.get("cur", "")
    if not rels:
        abort(400, "No files selected")
    dest_dir = safe_path(dest_rel) if dest_rel else safe_path(cur)
    if not dest_dir.exists():
        dest_dir.mkdir(parents=True, exist_ok=True)
    if not dest_dir.is_dir():
        abort(400, "Destination is not a folder")
    for rel in rels:
        src = safe_path(rel)
        if not src.is_file():
            continue
        dst = dest_dir / src.name
        if dst.resolve() == src.resolve():
            continue
        dst = unique_dest(dst)  # auto-rename on conflict
        delete_thumb(src)
        shutil.move(str(src), str(dst))
        ensure_thumb(dst)
    parent = dest_rel if dest_rel else cur
    return redirect(url_for("browse", path=parent))

def _ffconcat_line(p: Path) -> str:
    # concat-demuxer file list entry; forward slashes work on every OS
    # (Windows backslashes would be misread as escapes), single quotes
    # are escaped ffmpeg-style
    return "file '" + p.as_posix().replace("'", "'\\''") + "'\n"


@app.route("/bulk_merge", methods=["GET", "POST"])
def bulk_merge():
    if request.method == "GET":
        rels = request.args.getlist("selected")
        clips = [r for r in rels if Path(r).suffix.lower() in ALLOWED_VIDEO]
        if not clips:
            abort(400, "No video files selected")
        cur = request.args.get("cur_path", "") or (
            str(Path(clips[0]).parent) if str(Path(clips[0]).parent) != "." else "")
        return render_template_string(MERGE_HTML, files=clips, count=len(clips),
                                      cur=cur)
    rels = request.form.getlist("selected")
    clips = []
    for r in rels:
        p = safe_path(r)
        if p.is_file() and p.suffix.lower() in ALLOWED_VIDEO:
            clips.append(p)
    if not clips:
        abort(400, "No video files selected")
    if shutil.which("ffmpeg") is None:
        abort(500, "ffmpeg is not installed on the server. Install it with: sudo apt install ffmpeg")
    outname = Path(request.form.get("outname", "merged.mp4") or "merged.mp4").name
    if not outname.lower().endswith(".mp4"):
        outname += ".mp4"
    cur_path = request.form.get("cur_path", "")
    outdir = safe_path(cur_path)
    outdir.mkdir(parents=True, exist_ok=True)
    out = unique_dest(outdir / outname)
    listfile = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
            listfile = tf.name
            for c in clips:
                tf.write(_ffconcat_line(c))
        proc = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", listfile, "-c", "copy", str(out)],
            capture_output=True, text=True)
        if proc.returncode != 0:
            err = (proc.stderr or "")[-2000:]
            abort(500, f"ffmpeg failed to merge the clips: {err}")
    finally:
        if listfile and os.path.exists(listfile):
            os.unlink(listfile)
    return redirect(url_for("browse", path=cur_path))

@app.route("/upload", methods=["POST"])
def upload():
    cur_path = request.form.get("cur_path", "")
    dest = safe_path(cur_path)
    if not dest.is_dir():
        abort(400, "Upload destination is not a folder")
    saved = 0
    for f in request.files.getlist("files"):
        if not f or not f.filename:
            continue
        name = Path(f.filename.replace("\\", "/")).name  # strip any path parts
        if not name or name in (".", ".."):
            continue
        target = unique_dest(dest / name)
        f.save(str(target))
        ensure_thumb(target)  # create its thumbnail right away
        saved += 1
    if saved == 0:
        abort(400, "No files received")
    return redirect(url_for("browse", path=cur_path))

def main():
    global MEDIA_ROOT
    ap = argparse.ArgumentParser(description="Local media server (JPEG/MP4)")
    ap.add_argument("--dir", default=os.environ.get("MEDIA_DIR", "media"),
                    help="media directory to serve (default: ./media or $MEDIA_DIR)")
    ap.add_argument("--host", default="0.0.0.0", help="bind host (default 0.0.0.0 for remote access)")
    ap.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    args = ap.parse_args()
    MEDIA_ROOT = Path(args.dir).resolve()
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"Serving {MEDIA_ROOT} on http://{args.host}:{args.port}")
    print("Open http://<this-machine-ip>:%d from your remote browser." % args.port)
    app.run(host=args.host, port=args.port, threaded=True)

if __name__ == "__main__":
    main()
