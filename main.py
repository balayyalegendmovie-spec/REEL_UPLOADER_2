"""
============================================================
🎬 FULLY AUTOMATED INSTAGRAM REELS UPLOADER
============================================================
Template repo — clone for each Instagram account.
Only GitHub Secrets change between accounts.
============================================================
"""

import os, re, sys, json, time, random, shutil, subprocess, hashlib, requests, traceback
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta

os.environ["PYTHONUNBUFFERED"] = "1"

from PIL import Image, ImageDraw, ImageFont
from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired, ChallengeRequired, FeedbackRequired,
    PleaseWaitFewMinutes, ClientThrottledError,
)

GEMINI = False
try:
    from google import genai
    from google.genai import types as genai_types
    GEMINI = True
except ImportError:
    pass


# ── CONFIG ────────────────────────────────────────────────────
class C:
    IG_USER       = os.environ.get("IG_USERNAME", "")
    IG_PASS       = os.environ.get("IG_PASSWORD", "")
    IG_SESSION    = os.environ.get("IG_SESSION", "")
    DRIVE_FOLDER  = os.environ.get("GDRIVE_FOLDER_ID", "")
    DRIVE_KEY     = os.environ.get("GDRIVE_API_KEY", "")
    GEMINI_KEY    = os.environ.get("GEMINI_API_KEY", "")
    WATERMARK     = os.environ.get("WATERMARK_TEXT", "")
    LANGUAGE      = os.environ.get("CONTENT_LANGUAGE", "english").lower()

    CLIP_LEN      = 59
    MAX_PER_RUN   = 1
    MAX_ERRORS    = 3
    COOLDOWN_HRS  = 24
    ZOOM          = 0.03
    BRIGHT        = 0.02
    CONTRAST      = 1.02

    TMP           = "/tmp/reelbot"
    MOVIE_FILE    = f"{TMP}/movie.mp4"
    SESSION_FILE  = f"{TMP}/session.json"
    CLIPS_DIR     = f"{TMP}/clips"
    THUMBS_DIR    = f"{TMP}/thumbs"
    FRAMES_DIR    = f"{TMP}/frames"

    PROGRESS      = "progress.json"
    LOG           = "movies_log.json"
    HISTORY       = "upload_history.json"

    VIDEO_EXTS    = (".mp4", ".mkv", ".avi", ".mov", ".webm")
    FONT_BOLD     = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    FONT_REG      = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    GEMINI_MODELS = ["gemini-2.0-flash", "gemini-1.5-flash"]


# ── LOGGING ───────────────────────────────────────────────────
def log(msg, prefix="✅"):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {prefix} {msg}", flush=True)

def log_err(msg):  log(msg, "❌")
def log_warn(msg): log(msg, "⚠️")
def log_step(n, t, msg): print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ━━━ STEP {n}/{t}: {msg} ━━━", flush=True)


# ── JSON HELPERS ──────────────────────────────────────────────
def load_json(fp, default=None):
    if default is None: default = {}
    try:
        if os.path.exists(fp):
            with open(fp, "r") as f: return json.load(f)
    except Exception: pass
    return default

def save_json(fp, data):
    with open(fp, "w") as f: json.dump(data, f, indent=2, ensure_ascii=False)


# ── GIT ───────────────────────────────────────────────────────
def git_cmd(*args):
    try:
        return subprocess.run(["git"] + list(args), capture_output=True, text=True, timeout=60).returncode == 0
    except Exception: return False

def git_push():
    log("Pushing progress to GitHub...")
    git_cmd("config", "user.name", "ReelBot")
    git_cmd("config", "user.email", "bot@reelbot.com")
    ignore_entries = ["session.json", "*.mp4", "/tmp/", "thumb_cache/"]
    if os.path.exists(".gitignore"):
        existing = open(".gitignore").read()
    else:
        existing = ""
    new = [e for e in ignore_entries if e not in existing]
    if new:
        with open(".gitignore", "a") as f: f.write("\n".join([""] + new + [""]))
        git_cmd("add", ".gitignore")
    for f in [C.PROGRESS, C.LOG, C.HISTORY]:
        if os.path.exists(f): git_cmd("add", f)
    check = subprocess.run(["git", "diff", "--staged", "--quiet"], capture_output=True)
    if check.returncode != 0:
        git_cmd("commit", "-m", "🤖 progress update")
        git_cmd("push")
        log("Push complete")
    else:
        log("No changes to push")


# ── EPISODE PARSER ────────────────────────────────────────────
def parse_episode(filename):
    stem = Path(filename).stem
    s_m = re.search(r'[Ss](?:eason[_\s]?)?(\d+)', stem)
    e_m = re.search(r'[Ee]p(?:isode)?[_\s\-–]*(\d+)', stem)
    season = int(s_m.group(1)) if s_m else None
    episode = int(e_m.group(1)) if e_m else None

    clean = re.sub(r'[_]+', ' ', stem)
    clean = re.sub(r'\s*[–—-]\s*', ' – ', clean)
    clean = re.sub(r'\s*[–—-]?\s*(Tam|Tel|Hin|Eng|Sub|Dub)(\+(Tam|Tel|Hin|Eng|Sub|Dub))*\s*$', '', clean, flags=re.IGNORECASE)

    if episode is not None:
        sm = re.match(r'^(.*?)\s*[–—-]?\s*[Ee]pisode', clean)
        series = sm.group(1).strip() if sm else clean
        tm = re.search(r'[Ee]pisode\s+\d+\s*[–—-]\s*(.+)$', clean)
        title = tm.group(1).strip()[:30] if tm else ""
        display = f"{series} Ep.{episode}" + (f" – {title}" if title else "")
    else:
        display = clean.strip()

    display = re.sub(r'\s{2,}', ' ', display).strip()
    sort_key = (season or 9999, episode or 9999, filename)
    return {"display": display, "season": season, "episode": episode, "sort_key": sort_key}


# ── SMART JITTER ──────────────────────────────────────────────
def smart_delay():
    log_step(3, 9, "Smart jitter delay")
    history = load_json(C.HISTORY, {"uploads": []})
    now = datetime.now()
    hour = now.hour
    recent = [
        h["delay"] for h in history.get("uploads", [])
        if h.get("hour") == hour and
        (now - datetime.fromisoformat(h["time"])).days < 5
    ]
    candidates = [m for m in range(1, 16) if m not in recent]
    if not candidates:
        candidates = list(range(1, 16))
    delay = random.choice(candidates)
    log(f"Jitter: sleeping {delay} minutes (recent same-slot delays: {recent})")
    time.sleep(delay * 60)
    history.setdefault("uploads", []).append({
        "time": now.isoformat(), "hour": hour, "delay": delay
    })
    history["uploads"] = [
        h for h in history["uploads"]
        if (now - datetime.fromisoformat(h["time"])).days < 7
    ]
    save_json(C.HISTORY, history)
    log(f"Delayed {delay}min — uploading now")


# ── GOOGLE DRIVE ──────────────────────────────────────────────
def list_drive_files():
    log_step(4, 9, "Scan Google Drive")
    url = "https://www.googleapis.com/drive/v3/files"
    all_files, token = [], None
    while True:
        params = {
            "q": f"'{C.DRIVE_FOLDER}' in parents and trashed=false",
            "key": C.DRIVE_KEY,
            "fields": "nextPageToken,files(id,name,size)",
            "pageSize": 100,
        }
        if token: params["pageToken"] = token
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code != 200:
                log_err(f"Drive API {r.status_code}: {r.text[:200]}")
                return []
            data = r.json()
            for f in data.get("files", []):
                if any(f["name"].lower().endswith(e) for e in C.VIDEO_EXTS):
                    info = parse_episode(f["name"])
                    all_files.append({
                        "id": f["id"], "name": f["name"],
                        "size": int(f.get("size", 0)),
                        "display": info["display"], "sort_key": info["sort_key"],
                    })
            token = data.get("nextPageToken")
            if not token: break
        except Exception as e:
            log_err(f"Drive error: {e}")
            return []
    all_files.sort(key=lambda x: x["sort_key"])
    log(f"Found {len(all_files)} videos")
    for i, f in enumerate(all_files, 1):
        log(f"  {i}. {f['display']} ({f['size']/1024/1024:.1f}MB)")
    return all_files


def download_file(file_id, out_path):
    log_step(6, 9, "Download from Google Drive")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if os.path.exists(out_path): os.remove(out_path)
    for attempt in range(1, 4):
        try:
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&key={C.DRIVE_KEY}&acknowledgeAbuse=true"
            with requests.get(url, stream=True, timeout=600) as r:
                if r.status_code != 200:
                    log_err(f"Drive download HTTP {r.status_code}")
                    if attempt < 3: time.sleep(30 * attempt); continue
                    return False
                header = r.content[:20] if len(r.content) > 20 else r.content
                total = int(r.headers.get("content-length", 0))
                downloaded = 0
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total and downloaded % (50 * 1024 * 1024) < 8 * 1024 * 1024:
                            log(f"  Download: {downloaded/1024/1024:.0f}/{total/1024/1024:.0f} MB")
            if os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
                with open(out_path, "rb") as f:
                    head = f.read(50).lower()
                if b"<!doctype" in head or b"<html" in head:
                    log_err("Got HTML instead of video — file may be too large for API key")
                    os.remove(out_path)
                    return False
                log(f"Download complete: {os.path.getsize(out_path)/1024/1024:.1f} MB")
                return True
            log_err("Download produced empty file")
        except Exception as e:
            log_err(f"Download attempt {attempt} failed: {e}")
            if attempt < 3: time.sleep(30 * attempt)
    return False


# ── VIDEO PROCESSING ──────────────────────────────────────────
def get_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception: return 0.0

def count_parts(duration):
    return sum(1 for s in range(0, int(duration), C.CLIP_LEN)
               if min(s + C.CLIP_LEN, duration) - s >= 5)

def extract_clip(video, part, total, out_path, watermark="", display_name=""):
    start = (part - 1) * C.CLIP_LEN
    log(f"Extracting Part {part}/{total} ({start}s→{start+C.CLIP_LEN}s)")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    part_file = f"{C.TMP}/part_text.txt"
    wm_file = f"{C.TMP}/wm_text.txt"
    with open(part_file, "w") as f: f.write(f"Part {part}/{total}")
    with open(wm_file, "w") as f: f.write(watermark if watermark else " ")

    vf_parts = [
        f"scale=trunc(1080*(1+{C.ZOOM})/2)*2:-2",
        f"crop=1080:trunc(ih*1080/(iw)/2)*2",
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
        f"eq=brightness={C.BRIGHT}:contrast={C.CONTRAST}",
    ]
    if os.path.exists(C.FONT_BOLD):
        font_esc = C.FONT_BOLD.replace(":", "\\:")
        vf_parts.append(
            f"drawtext=textfile='{part_file}':fontfile='{font_esc}'"
            f":fontsize=36:fontcolor=white:x=w-tw-30:y=30"
            f":box=1:boxcolor=black@0.5:boxborderw=10"
        )
        if watermark:
            vf_parts.append(
                f"drawtext=textfile='{wm_file}':fontfile='{font_esc}'"
                f":fontsize=28:fontcolor=white@0.4:x=w-tw-30:y=h-th-30"
                f":shadowcolor=black@0.6:shadowx=2:shadowy=2"
            )
    vf = ",".join(vf_parts)

    cmd = [
        "ffmpeg", "-y", "-ss", str(start), "-i", video,
        "-t", str(C.CLIP_LEN), "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", out_path,
    ]
    try:
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            log_warn("Retrying without audio...")
            cmd_no_audio = [
                "ffmpeg", "-y", "-ss", str(start), "-i", video,
                "-t", str(C.CLIP_LEN), "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-an", "-movflags", "+faststart", out_path,
            ]
            r = subprocess.run(cmd_no_audio, capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                log_err(f"ffmpeg failed: {r.stderr[-300:]}")
                return False
        if os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
            log(f"Clip ready in {time.time()-t0:.1f}s — {os.path.getsize(out_path)/1024/1024:.1f}MB")
            return True
        log_err("ffmpeg produced empty file")
        return False
    except subprocess.TimeoutExpired:
        log_err("ffmpeg timeout")
        return False
    except Exception as e:
        log_err(f"Clip extraction error: {e}")
        return False

def validate_clip(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30)
        info = json.loads(r.stdout)
        dur = float(info.get("format", {}).get("duration", 0))
        if dur > 60: log_err(f"Clip too long: {dur:.1f}s"); return False
        if dur < 3: log_err(f"Clip too short: {dur:.1f}s"); return False
        log(f"Clip valid: {dur:.1f}s")
        return True
    except Exception as e:
        log_warn(f"Validation skipped: {e}")
        return True


# ── THUMBNAIL ─────────────────────────────────────────────────
def extract_frame(video, t_sec, out_jpg):
    os.makedirs(os.path.dirname(out_jpg), exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(t_sec), "-i", video,
         "-frames:v", "1", "-q:v", "2", out_jpg],
        capture_output=True, timeout=30)
    if os.path.exists(out_jpg) and os.path.getsize(out_jpg) > 0:
        return Image.open(out_jpg).copy()
    return Image.new("RGB", (1280, 720), (20, 20, 40))

def select_best_frame(video, duration):
    log("Selecting best thumbnail frame...")
    frames, timestamps = [], []
    for i in range(9):
        t = min(duration * (0.1 + i * 0.08), duration - 1.0)
        timestamps.append(t)
        jpg = os.path.join(C.FRAMES_DIR, f"frame_{i}.jpg")
        frames.append(extract_frame(video, t, jpg))

    chosen_idx = 4
    if GEMINI and C.GEMINI_KEY:
        try:
            grid = Image.new("RGB", (960, 960))
            for idx, img in enumerate(frames):
                grid.paste(img.resize((320, 320)), ((idx % 3) * 320, (idx // 3) * 320))
            buf = BytesIO()
            grid.save(buf, format="JPEG", quality=85)
            client = genai.Client(api_key=C.GEMINI_KEY)
            for model in C.GEMINI_MODELS:
                try:
                    resp = client.models.generate_content(
                        model=model,
                        contents=[
                            genai_types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"),
                            genai_types.Part.from_text(text=(
                                "Pick the best movie thumbnail frame from this 3x3 grid.\n"
                                "Grid numbered: 1 2 3 / 4 5 6 / 7 8 9\n"
                                "Choose the brightest, clearest frame with visible characters.\n"
                                "Reply with ONLY a single digit 1-9."
                            )),
                        ],
                    )
                    d = next((c for c in resp.text.strip() if c.isdigit() and c != "0"), None)
                    if d and 1 <= int(d) <= 9:
                        chosen_idx = int(d) - 1
                        log(f"Gemini chose frame #{d}")
                    break
                except Exception as e:
                    log_warn(f"Gemini {model}: {e}")
        except Exception as e:
            log_warn(f"Gemini init failed: {e}")

    shutil.rmtree(C.FRAMES_DIR, ignore_errors=True)
    chosen_time = timestamps[chosen_idx]
    log(f"Best frame at t={chosen_time:.1f}s")
    return frames[chosen_idx], chosen_time

def get_font(size, bold=True):
    fp = C.FONT_BOLD if bold else C.FONT_REG
    try:
        if os.path.exists(fp): return ImageFont.truetype(fp, size)
    except Exception: pass
    return ImageFont.load_default()

def make_thumbnail(bg_img, display_name, part, total, out_path):
    log(f"Creating thumbnail Part {part}/{total}")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    try:
        thumb = bg_img.copy().resize((1080, 1920), Image.LANCZOS).convert("RGBA")
        overlay = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
        draw_o = ImageDraw.Draw(overlay)
        for y in range(600):
            draw_o.rectangle([(0, y), (1080, y+1)], fill=(0, 0, 0, int(200 * (1 - y/600))))
        for y in range(1400, 1920):
            draw_o.rectangle([(0, y), (1080, y+1)], fill=(0, 0, 0, int(200 * ((y-1400)/520))))
        thumb = Image.alpha_composite(thumb, overlay).convert("RGB")
        draw = ImageDraw.Draw(thumb)

        font_title = get_font(64)
        font_part = get_font(56)

        title = display_name.upper()
        words, lines, line = title.split(), [], ""
        for w in words:
            test = (line + " " + w).strip()
            if len(test) > 20 and line: lines.append(line); line = w
            else: line = test
        if line: lines.append(line)

        y_cur = 80
        for ln in lines:
            bb = draw.textbbox((0, 0), ln, font=font_title)
            tw = bb[2] - bb[0]
            x = (1080 - tw) // 2
            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    draw.text((x+dx, y_cur+dy), ln, font=font_title, fill="black")
            draw.text((x, y_cur), ln, font=font_title, fill="white")
            y_cur += bb[3] - bb[1] + 14

        pt = f"PART {part} / {total}"
        bb = draw.textbbox((0, 0), pt, font=font_part)
        x = (1080 - (bb[2] - bb[0])) // 2
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                draw.text((x+dx, 1760+dy), pt, font=font_part, fill="black")
        draw.text((x, 1760), pt, font=font_part, fill=(255, 215, 0))

        draw.rectangle([(200, 1740), (880, 1743)], fill=(255, 215, 0))

        thumb.save(out_path, "JPEG", quality=95)
        log("Thumbnail saved")
        return True
    except Exception as e:
        log_err(f"Thumbnail error: {e}")
        try:
            fb = Image.new("RGB", (1080, 1920), (20, 20, 40))
            d = ImageDraw.Draw(fb)
            f = get_font(48)
            d.text((100, 800), display_name, font=f, fill="white")
            d.text((100, 900), f"Part {part}/{total}", font=f, fill=(255, 215, 0))
            fb.save(out_path, "JPEG")
            return True
        except Exception: return False


# ── GEMINI CAPTIONS ───────────────────────────────────────────
def generate_caption(display_name, part, total):
    lang = C.LANGUAGE
    if GEMINI and C.GEMINI_KEY:
        try:
            client = genai.Client(api_key=C.GEMINI_KEY)
            prompt = (
                f"Generate an Instagram Reels caption for:\n"
                f"  Show: {display_name}\n"
                f"  Language: {lang}\n"
                f"  Part: {part} of {total}\n\n"
                f"Rules:\n"
                f"- Write in {lang} script (use native script not transliteration)\n"
                f"- Start with emoji + curiosity hook\n"
                f"- Add a question or call-to-action for comments\n"
                f"- Include 'Follow for next part' in {lang}\n"
                f"- Add 3 dots/lines before hashtags\n"
                f"- Add 20 hashtags mixing {lang} + English\n"
                f"- Include these trending tags: #reels #viral #trending #fyp\n"
                f"- Keep under 2000 characters\n"
                f"- Make it exciting with emojis\n"
                f"- Vary style: question / cliffhanger / nostalgia\n"
            )
            for model in C.GEMINI_MODELS:
                try:
                    resp = client.models.generate_content(model=model, contents=prompt)
                    if resp.text and len(resp.text) > 50:
                        log(f"Gemini caption generated ({model})")
                        return resp.text.strip()
                except Exception as e:
                    log_warn(f"Gemini caption {model}: {e}")
        except Exception as e:
            log_warn(f"Gemini caption init: {e}")

    # Fallback templates
    templates = {
        "telugu": [
            "😱 {name} చూడండి!\nPart {p}/{t}\n\nNext part కోసం Follow చేయండి! 🔔\n.\n.\n.\n#doraemon #telugu #telugucartons #telugureels #viral #trending #fyp #reels #anime #cartoon",
            "🔥 {name} — Part {p}/{t}\n\n❤️ Like & Follow for more!\n.\n.\n.\n#doraemon #telugu #trending #viral #reels #fyp #cartoonstelugu #teluguanimation",
            "🎬 {name} [{p}/{t}]\n\nమీకు నచ్చితే Like చేయండి! 👇\n.\n.\n.\n#telugu #doraemon #cartoon #viral #trending #reels #fyp #telugucartons",
        ],
        "tamil": [
            "😱 {name} பாருங்க!\nPart {p}/{t}\n\nNext part க்கு Follow பண்ணுங்க! 🔔\n.\n.\n.\n#doraemon #tamil #tamilcartoon #tamilreels #viral #trending #fyp #reels #anime",
            "🔥 {name} — Part {p}/{t}\n\n❤️ Like & Follow!\n.\n.\n.\n#doraemon #tamil #trending #viral #reels #fyp #cartoonstamil #tamilanimation",
            "🎬 {name} [{p}/{t}]\n\nஉங்களுக்கு பிடித்ததா? Like போடுங்க! 👇\n.\n.\n.\n#tamil #doraemon #cartoon #viral #trending #reels #fyp",
        ],
        "hindi": [
            "😱 {name} देखो!\nPart {p}/{t}\n\nNext part के लिए Follow करो! 🔔\n.\n.\n.\n#doraemon #hindi #hindicartoon #hindireels #viral #trending #fyp #reels #anime",
            "🔥 {name} — Part {p}/{t}\n\n❤️ Like & Follow करो!\n.\n.\n.\n#doraemon #hindi #trending #viral #reels #fyp #cartoonhindi",
            "🎬 {name} [{p}/{t}]\n\nपसंद आया तो Like करो! 👇\n.\n.\n.\n#hindi #doraemon #cartoon #viral #trending #reels #fyp",
        ],
    }
    pool = templates.get(lang, templates.get("english", [
        "🎬 {name} Part {p}/{t}\n\nFollow for next part! 🔔\n.\n.\n.\n#movie #reels #viral #trending #fyp #cinema"
    ]))
    return random.choice(pool).format(name=display_name, p=part, t=total)


# ── INSTAGRAM ─────────────────────────────────────────────────
def ig_login():
    log("Instagram login via session...")
    if not os.path.exists(C.SESSION_FILE):
        log_err("session.json not found")
        return None, None
    try:
        cl = Client()
        cl.delay_range = [3, 7]
        cl.load_settings(C.SESSION_FILE)
        cl.login(C.IG_USER, C.IG_PASS)
        cl.get_timeline_feed()
        log("Instagram session valid")
        return cl, None
    except ChallengeRequired:
        log_err("Instagram challenge — session needs regeneration")
        return None, "challenge"
    except LoginRequired:
        log_err("Session expired — regenerate session.json locally")
        return None, "expired"
    except Exception as e:
        log_err(f"Instagram login failed: {e}")
        return None, "error"

def ig_upload(cl, clip_path, thumb_path, caption):
    log(f"Uploading to Instagram ({os.path.getsize(clip_path)/1024/1024:.1f}MB)...")
    for attempt in range(1, 4):
        try:
            time.sleep(random.randint(10, 30))
            kwargs = {"path": clip_path, "caption": caption}
            if thumb_path and os.path.exists(thumb_path):
                kwargs["thumbnail"] = Path(thumb_path)
            cl.clip_upload(**kwargs)
            log(f"Instagram upload SUCCESS (attempt {attempt})")
            return True
        except PleaseWaitFewMinutes:
            log_warn(f"Rate limited — waiting {10*attempt} min...")
            time.sleep(600 * attempt)
        except ClientThrottledError:
            log_warn(f"Throttled — waiting {15*attempt} min...")
            time.sleep(900 * attempt)
        except FeedbackRequired as e:
            log_err(f"FeedbackRequired: {e}")
            return "challenge"
        except ChallengeRequired:
            log_err("Challenge during upload")
            return "challenge"
        except LoginRequired:
            log_err("Session expired during upload")
            return "challenge"
        except Exception as e:
            log_err(f"Upload attempt {attempt}: {e}")
            if attempt < 3: time.sleep(120 * attempt)
    log_err("Upload failed after 3 attempts")
    return False


# ── STATE MANAGEMENT ──────────────────────────────────────────
def load_log():
    return load_json(C.LOG, {"videos": {}, "order": [], "completed": 0, "uploaded": 0})

def save_log(data):
    data["completed"] = sum(1 for v in data["videos"].values() if v["status"] == "completed")
    data["uploaded"] = sum(v.get("parts_done", 0) for v in data["videos"].values())
    data["last_run"] = datetime.now().isoformat()
    save_json(C.LOG, data)

def sync_log(log_data, drive_files):
    id_map = {}
    order = []
    for f in drive_files:
        did = f["id"]
        order.append(did)
        id_map[did] = f
        if did not in log_data["videos"]:
            log_data["videos"][did] = {
                "status": "pending", "total_parts": 0, "parts_done": 0,
                "errors": 0, "started": "", "completed_at": "",
            }
            log(f"New video: {f['display']}")
    log_data["order"] = order
    return log_data, id_map

def get_next(log_data):
    for did in log_data.get("order", []):
        v = log_data["videos"].get(did, {})
        if v.get("status") == "in_progress": return did, v
    for did in log_data.get("order", []):
        v = log_data["videos"].get(did, {})
        if v.get("status") == "pending": return did, v
    return None, None

def load_progress():
    return load_json(C.PROGRESS, {
        "drive_id": "", "part": 0, "total": 0,
        "thumb_time": -1, "cooldown_until": ""
    })

def save_progress(p):
    save_json(C.PROGRESS, p)

def check_cooldown(progress):
    cd = progress.get("cooldown_until", "")
    if cd:
        try:
            until = datetime.fromisoformat(cd)
            if datetime.now() < until:
                left = (until - datetime.now()).total_seconds() / 3600
                log_warn(f"Cooldown active — {left:.1f}h remaining. Skipping run.")
                return True
        except Exception: pass
    return False


# ── SETUP ─────────────────────────────────────────────────────
def setup():
    log_step(1, 9, "Setup environment")
    for d in [C.TMP, C.CLIPS_DIR, C.THUMBS_DIR, C.FRAMES_DIR]:
        os.makedirs(d, exist_ok=True)

    if C.IG_SESSION.strip():
        try:
            parsed = json.loads(C.IG_SESSION)
            with open(C.SESSION_FILE, "w") as f:
                json.dump(parsed, f, indent=2)
            log("Session written from secret")
        except json.JSONDecodeError:
            log_err("IG_SESSION secret is not valid JSON")
            return False

    log_step(2, 9, "Verify secrets")
    missing = []
    for val, name in [(C.IG_USER, "IG_USERNAME"), (C.IG_PASS, "IG_PASSWORD"),
                       (C.DRIVE_FOLDER, "GDRIVE_FOLDER_ID"), (C.DRIVE_KEY, "GDRIVE_API_KEY")]:
        if val: log(f"  ✓ {name}")
        else: log_err(f"  ✗ {name} MISSING"); missing.append(name)
    log(f"  {'✓' if C.GEMINI_KEY else '~'} GEMINI_API_KEY")
    log(f"  {'✓' if C.WATERMARK else '~'} WATERMARK_TEXT = '{C.WATERMARK}'")
    log(f"  ✓ LANGUAGE = '{C.LANGUAGE}'")
    if missing:
        log_err(f"Missing secrets: {', '.join(missing)}")
        return False
    return True


# ── MAIN ──────────────────────────────────────────────────────
def main():
    print("=" * 60, flush=True)
    print(f"🎬 INSTAGRAM REELS AUTO UPLOADER — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print("=" * 60, flush=True)

    if not setup():
        return

    progress = load_progress()
    if check_cooldown(progress):
        return

    smart_delay()

    drive_files = list_drive_files()
    if not drive_files:
        log_err("No videos in Drive folder")
        return

    log_step(5, 9, "Sync tracker & select next video")
    log_data = load_log()
    log_data, id_map = sync_log(log_data, drive_files)
    save_log(log_data)

    drive_id, video_info = get_next(log_data)
    if not drive_id:
        log("🎉 All videos fully uploaded!")
        return

    file_meta = id_map.get(drive_id)
    if not file_meta:
        log_err(f"Drive ID {drive_id} not found in current scan")
        return
    display = file_meta["display"]
    log(f"Selected: {display}")

    if not download_file(drive_id, C.MOVIE_FILE):
        video_info["errors"] = video_info.get("errors", 0) + 1
        if video_info["errors"] >= C.MAX_ERRORS:
            video_info["status"] = "error"
            log_err(f"Marking as error after {C.MAX_ERRORS} failures")
        save_log(log_data); git_push()
        return

    log_step(7, 9, "Analyze video")
    duration = get_duration(C.MOVIE_FILE)
    if duration <= 0:
        video_info["status"] = "error"; save_log(log_data); git_push(); return
    total = count_parts(duration)
    log(f"Duration: {duration:.0f}s = {total} parts × {C.CLIP_LEN}s")
    video_info["total_parts"] = total
    if video_info["status"] == "pending":
        video_info["status"] = "in_progress"
        video_info["started"] = datetime.now().isoformat()
    save_log(log_data)

    if progress.get("drive_id") != drive_id:
        progress = {"drive_id": drive_id, "part": 0, "total": total,
                     "thumb_time": -1, "cooldown_until": ""}
    last = progress["part"]
    log(f"Progress: {last}/{total} done")

    if last >= total:
        log("🎉 Already completed — marking done")
        video_info["status"] = "completed"
        video_info["completed_at"] = datetime.now().isoformat()
        progress = {"drive_id": "", "part": 0, "total": 0, "thumb_time": -1, "cooldown_until": ""}
        save_progress(progress); save_log(log_data); git_push(); return

    # ── Thumbnail frame (one per episode) ──
    log_step(8, 9, "Thumbnail frame selection")
    if progress.get("thumb_time", -1) < 0:
        bg_frame, thumb_time = select_best_frame(C.MOVIE_FILE, duration)
        progress["thumb_time"] = thumb_time
        save_progress(progress)
    else:
        thumb_time = progress["thumb_time"]
        jpg = os.path.join(C.THUMBS_DIR, "bg.jpg")
        bg_frame = extract_frame(C.MOVIE_FILE, thumb_time, jpg)
        log(f"Reusing saved frame at t={thumb_time:.1f}s")

    # ── Instagram login ──
    log_step(9, 9, "Upload to Instagram")
    cl, login_err = ig_login()
    if login_err == "challenge":
        cd_until = (datetime.now() + timedelta(hours=C.COOLDOWN_HRS)).isoformat()
        progress["cooldown_until"] = cd_until
        log_err(f"Challenge → cooldown until {cd_until}")
        save_progress(progress); save_log(log_data); git_push(); return
    if cl is None:
        log_err("Login failed — aborting")
        save_progress(progress); save_log(log_data); git_push(); return

    # ── Extract, thumbnail, upload ──
    part = last + 1
    clip_path = os.path.join(C.CLIPS_DIR, f"part_{part}.mp4")
    thumb_path = os.path.join(C.THUMBS_DIR, f"thumb_{part}.jpg")

    if not extract_clip(C.MOVIE_FILE, part, total, clip_path, C.WATERMARK, display):
        video_info["errors"] = video_info.get("errors", 0) + 1
        if video_info["errors"] >= C.MAX_ERRORS:
            video_info["status"] = "error"
        save_progress(progress); save_log(log_data); git_push(); return

    if not validate_clip(clip_path):
        video_info["errors"] = video_info.get("errors", 0) + 1
        if video_info["errors"] >= C.MAX_ERRORS:
            video_info["status"] = "error"
        save_progress(progress); save_log(log_data); git_push(); return

    make_thumbnail(bg_frame, display, part, total, thumb_path)
    caption = generate_caption(display, part, total)

    result = ig_upload(cl, clip_path, thumb_path, caption)

    if result == "challenge":
        cd_until = (datetime.now() + timedelta(hours=C.COOLDOWN_HRS)).isoformat()
        progress["cooldown_until"] = cd_until
        log_err(f"Challenge during upload → cooldown until {cd_until}")
        save_progress(progress); save_log(log_data); git_push(); return

    if result is True:
        progress["part"] = part
        video_info["parts_done"] = part
        video_info["errors"] = 0
        log(f"✅ Part {part}/{total} uploaded successfully!")

        if part >= total:
            log("🎉🎉🎉 VIDEO FULLY UPLOADED! 🎉🎉🎉")
            video_info["status"] = "completed"
            video_info["completed_at"] = datetime.now().isoformat()
            progress = {"drive_id": "", "part": 0, "total": 0,
                        "thumb_time": -1, "cooldown_until": ""}
            order = log_data.get("order", [])
            idx = order.index(drive_id) if drive_id in order else -1
            if idx >= 0 and idx + 1 < len(order):
                next_id = order[idx + 1]
                next_meta = id_map.get(next_id)
                if next_meta:
                    log(f"⏭️ Next: {next_meta['display']}")
            elif idx >= 0:
                log("🏆 That was the LAST video!")
        else:
            remaining = total - part
            hours_left = remaining * 2
            log(f"{remaining} parts left (~{hours_left}h at 12/day)")
    else:
        video_info["errors"] = video_info.get("errors", 0) + 1
        log_err(f"Upload failed (errors: {video_info['errors']}/{C.MAX_ERRORS})")
        if video_info["errors"] >= C.MAX_ERRORS:
            video_info["status"] = "error"
            log_err("Max errors reached — skipping to next video")

    save_progress(progress)
    save_log(log_data)
    git_push()

    # ── Summary ──
    print("\n" + "=" * 50, flush=True)
    total_v = len(log_data["videos"])
    done_v = log_data.get("completed", 0)
    total_r = log_data.get("uploaded", 0)
    print(f"📊 Videos: {done_v}/{total_v} done | Reels: {total_r} uploaded", flush=True)
    print("=" * 50, flush=True)

    # Cleanup temp
    shutil.rmtree(C.TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log_warn("Interrupted")
        git_push()
    except Exception as e:
        log_err(f"CRITICAL: {e}")
        log_err(traceback.format_exc())
        git_push()
        sys.exit(1)
