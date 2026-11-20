"""
BharatFacts automated Shorts pipeline (animated version)

  1. Claude writes a ~50s script split into 6-8 scenes, each with a picture prompt
  2. Cloudflare Workers AI (free tier) draws a cartoon picture for every scene
  3. ElevenLabs reads the script and returns word timings
  4. renderer.py animates it: camera moves, transitions, word-by-word captions
  5. Uploads to YouTube if OAuth secrets are set

Run:  python youtube_automation.py english 0      (language, slot number for the day)
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from renderer import MOTIONS, Scene, Word, render

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")

VOICE_EN = "21m00Tcm4TlvDq8ikWAM"   # Rachel
VOICE_HI = "pNInz6obpgDQGcFmaJgB"   # Adam (multilingual v2 handles Hindi)

# One fixed look so every scene and every video feels like the same channel.
STYLE = ("Flat 2D cartoon illustration, bold clean outlines, soft cel shading, vibrant saffron, teal and cream "
         "colour palette, simple rounded shapes, expressive friendly characters, detailed but uncluttered "
         "background, educational animation style, main subject centred in the frame, no text, no letters, no logos, no watermark")

# Generic, brand-free topics (no game/film characters or real people - those get copyright claims).
TOPICS_EN = [
    "Why India has only one time zone", "The stepwell that is 13 storeys deep",
    "How the Indian Railways feeds millions every day", "The village where every house has no door",
    "Why monsoon rains arrive almost on the same date every year", "How Chandrayaan landed near the Moon's south pole",
    "The world's highest cricket ground", "Why India has 22 official languages",
    "How the number zero changed the world", "The floating post office on Dal Lake",
    "Why Mumbai's dabbawalas almost never make mistakes", "The living root bridges of Meghalaya",
    "How chess was born in India", "Why the Taj Mahal changes colour during the day",
    "The town where it rains more than anywhere on Earth", "How UPI made India the world leader in digital payments",
    "The 3,000-year-old iron pillar that never rusts", "Why peacocks became India's national bird",
    "How AI is predicting monsoon floods", "5 things your phone knows about you",
    "Why your phone battery dies faster in winter", "How robots are farming in India",
    "The temple kitchen that feeds 100,000 people a day", "Why the Ganges is called a self-cleaning river",
    "How satellites help farmers decide when to sow", "The Indian island with its own tribe nobody may visit",
    "Why Indian trains have yellow and white stripes", "How ISRO sends missions for less than a movie budget",
    "The desert festival lit by thousands of lamps", "Why ChatGPT sometimes makes things up",
]


# --- 1. Script + scene plan (Claude) -------------------------------------------
def plan_video(topic: str, language: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    lang = ("Write the narration in natural spoken Hindi using Devanagari script (simple everyday words, "
            "English terms like UPI/AI are fine)." if language == "hindi"
            else "Write the narration in simple, punchy spoken English.")
    prompt = f"""You write scripts for BharatFacts, a faceless YouTube Shorts channel of fun animated facts.
Topic: {topic}

{lang}
Rules:
- Total narration 110-130 words (about 45-55 seconds when spoken). Only state facts you are confident are true.
- First sentence is a hook: a surprising fact or question that makes people keep watching.
- Split into 6-8 scenes. Each scene = 1-2 short sentences of narration.
- Last scene ends with a short call to follow for more facts.
- For each scene write image_prompt in ENGLISH: one vivid, concrete visual moment that matches the narration
  (who/what, action, setting, camera angle). Reuse the same recurring narrator character where it fits:
  "a cheerful young Indian guide in a teal hoodie". Never describe real people, celebrities, brands, logos,
  video-game or movie characters, flags with text, or any written words.
- hook_text: 2-5 word on-screen hook in the same language as the narration.

Return ONLY JSON, no markdown:
{{"title": "...max 70 chars, ends with #Shorts", "description": "2-3 sentences", "tags": ["..."],
  "hook_text": "...", "scenes": [{{"narration": "...", "image_prompt": "..."}}]}}"""
    msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=2000,
                                 messages=[{"role": "user", "content": prompt}])
    text = msg.content[0].text.strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.M).strip()
    plan = json.loads(text)
    assert 4 <= len(plan["scenes"]) <= 10, "unexpected scene count"
    return plan


# --- 2. Scene pictures (Cloudflare Workers AI, free tier) ---------------------
# FLUX schnell costs ~80 of the 10,000 free daily "neurons" per picture, so 24 pictures/day stay free.
# (Leonardo Phoenix looks similar but costs ~4,000 per picture - it burns the free quota in 2 images.)
CF_MODELS = [
    ("@cf/black-forest-labs/flux-1-schnell", {"steps": 6}),
]
_cf_quota_hit = False


def cf_image(prompt: str, dest: Path) -> bool:
    global _cf_quota_hit
    if not (CF_ACCOUNT_ID and CF_API_TOKEN) or _cf_quota_hit:
        return False
    for model, extra in CF_MODELS:
        for attempt in range(2):
            try:
                r = requests.post(
                    f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{model}",
                    headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
                    json={"prompt": f"{prompt}. {STYLE}", **extra}, timeout=120)
                if r.status_code == 200:
                    if "json" in r.headers.get("Content-Type", ""):
                        dest.write_bytes(base64.b64decode(r.json()["result"]["image"]))
                    else:
                        dest.write_bytes(r.content)
                    return True
                print(f"   {model} -> HTTP {r.status_code}: {r.text[:150]}")
                if r.status_code == 429:  # daily free quota used up - stop asking for today
                    _cf_quota_hit = True
                    return False
                if r.status_code in (400, 401, 403, 404):
                    break  # not retryable on this model
            except requests.RequestException as e:
                print(f"   {model} error: {e}")
            time.sleep(3)
    return False


def pixabay_image(query: str, dest: Path) -> bool:
    """Fallback if Cloudflare quota is used up: an illustration from Pixabay."""
    if not PIXABAY_API_KEY:
        return False
    r = requests.get("https://pixabay.com/api/", params={
        "key": PIXABAY_API_KEY, "q": " ".join(query.split()[:5]), "image_type": "illustration",
        "orientation": "vertical", "safesearch": "true", "per_page": 5}, timeout=30)
    hits = r.json().get("hits", []) if r.ok else []
    if not hits:
        return False
    dest.write_bytes(requests.get(hits[0]["largeImageURL"], timeout=60).content)
    return True


# --- 3. Voiceover with word timings (ElevenLabs) ------------------------------
def voiceover(text: str, language: str, dest: Path) -> list[Word]:
    voice = VOICE_HI if language == "hindi" else VOICE_EN
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps",
        headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_multilingual_v2",
              "voice_settings": {"stability": 0.45, "similarity_boost": 0.75, "style": 0.3}},
        timeout=180)
    r.raise_for_status()
    data = r.json()
    dest.write_bytes(base64.b64decode(data["audio_base64"]))
    al = data.get("alignment") or data.get("normalized_alignment")
    chars, starts, ends = al["characters"], al["character_start_times_seconds"], al["character_end_times_seconds"]
    words, cur, ws = [], "", None
    for c, s, e in zip(chars, starts, ends):
        if c.isspace():
            if cur:
                words.append(Word(cur, ws, last_end))
            cur, ws = "", None
            continue
        if ws is None:
            ws = s
        cur += c
        last_end = e
    if cur:
        words.append(Word(cur, ws, last_end))
    return words


# --- 4. Assemble --------------------------------------------------------------
def build(plan: dict, language: str, out: Path) -> Path:
    scenes_in = plan["scenes"]
    narration = " ".join(s["narration"].strip() for s in scenes_in)

    print("2/4 Voiceover + timings...")
    audio = out / "voiceover.mp3"
    words = voiceover(narration, language, audio)

    # map each scene to its first word to get scene start times
    counts = [len(s["narration"].split()) for s in scenes_in]
    starts, idx = [], 0
    for c in counts:
        starts.append(words[min(idx, len(words) - 1)].start)
        idx += c
    starts[0] = 0.0
    total = words[-1].end + 0.6

    print("3/4 Drawing scenes...")
    scenes = []
    for i, s in enumerate(scenes_in):
        img = out / f"scene_{i}.jpg"
        ok = cf_image(s["image_prompt"], img) or pixabay_image(s["image_prompt"], img)
        if not ok:
            if scenes:  # reuse the previous picture with a different camera move
                img = scenes[-1].image
            else:
                raise RuntimeError("Could not get any scene image")
        end = starts[i + 1] if i + 1 < len(starts) else total
        scenes.append(Scene(img, starts[i], end, MOTIONS[i % len(MOTIONS)]))
        print(f"   scene {i + 1}/{len(scenes_in)} ok")

    print("4/4 Animating...")
    final = out / "final_video.mp4"
    render(scenes, words, audio, final, hook=plan.get("hook_text", ""))
    return final


# --- 5. YouTube upload (OAuth refresh token) ----------------------------------
def upload_to_youtube(video: Path, plan: dict, language: str) -> str | None:
    cid, csec, rtok = (os.environ.get(k, "") for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"))
    if not (cid and csec and rtok):
        print(" YouTube secrets not set - video kept as a workflow artifact only.")
        return None
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build as gbuild
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(None, refresh_token=rtok, client_id=cid, client_secret=csec,
                        token_uri="https://oauth2.googleapis.com/token",
                        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    yt = gbuild("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {
            "title": plan["title"][:100],
            "description": f"{plan['description']}\n\n#Shorts #BharatFacts #Facts",
            "tags": (plan.get("tags") or [])[:15] + ["BharatFacts", "Shorts"],
            "categoryId": "27",
            "defaultLanguage": "hi" if language == "hindi" else "en",
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False,
                   "containsSyntheticMedia": True},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    print(f"  OK Uploaded: https://youtube.com/shorts/{resp['id']}")
    return resp["id"]


def pick_topic(language: str, slot: int) -> str:
    # Hindi videos use the same topic list (Claude writes the Hindi script); offset so it differs from English slots
    day = datetime.now(timezone.utc).timetuple().tm_yday
    return TOPICS_EN[(day * 3 + slot + (11 if language == "hindi" else 0)) % len(TOPICS_EN)]


def run(language: str = "english", slot: int = 0) -> dict:
    out = Path("output") / f"{datetime.now():%Y%m%d_%H%M%S}_{language}"
    out.mkdir(parents=True, exist_ok=True)
    topic = os.environ.get("TOPIC") or pick_topic(language, slot)
    print(f"\n {topic}  ({language})\n1/4 Planning scenes...")
    plan = plan_video(topic, language)
    (out / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    final = build(plan, language, out)
    vid = upload_to_youtube(final, plan, language)
    return {"topic": topic, "title": plan["title"], "video": str(final), "youtube_id": vid}


if __name__ == "__main__":
    lang = sys.argv[1] if len(sys.argv) > 1 else "english"
    slot = int(sys.argv[2]) if len(sys.argv) > 2 else random.randint(0, 2)
    print(json.dumps(run(lang, slot), ensure_ascii=False, indent=2))
