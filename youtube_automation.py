"""
BharatFacts Automated YouTube Channel
Generates faceless videos daily and saves them for upload
Topics: AI, tech, viral news, GTA 6, Indian facts
"""

import os
import random
import subprocess
import json
import requests
from pathlib import Path
from datetime import datetime

# ─── API Keys (set these as GitHub Secrets or env vars) ───────────────────────
CLAUDE_API_KEY     = os.environ.get("CLAUDE_API_KEY", "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
PIXABAY_API_KEY    = os.environ.get("PIXABAY_API_KEY", "")
GOOGLE_API_KEY     = os.environ.get("GOOGLE_API_KEY", "")
YOUTUBE_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "UCpgR1Kywm5jBs967EUVEt2Q")  # BharatFacts Hindi

# ─── ElevenLabs Voice IDs ──────────────────────────────────────────────────────
VOICE_EN = "21m00Tcm4TlvDq8ikWAM"   # Rachel – clear English
VOICE_HI = "pNInz6obpgDQGcFmaJgB"   # Adam  – works well for Hindi-accented English

# ─── Topic Pool ────────────────────────────────────────────────────────────────
TOPICS_EN = [
    "GTA 6 – every secret and hidden detail explained",
    "5 AI facts that will blow your mind",
    "Why GTA 6 will change gaming forever",
    "The viral fox video – what really happened",
    "Top 5 technologies that will replace humans by 2030",
    "India's secret space missions you never heard about",
    "5 things nobody tells you about ChatGPT",
    "The real story behind the most viral meme of 2026",
    "GTA 6 Lucia's full backstory explained",
    "5 Indian inventions that changed the world",
]

TOPICS_HI = [
    "GTA 6 ke har raaz ka explanation",
    "5 AI facts jo aapka dimaag hila denge",
    "GTA 6 kyu gaming ko badal dega",
    "Viral fox video ki poori sachchi kahani",
    "2030 tak ye 5 technologies humans ko replace kar denge",
    "India ke secret space missions jo aapne kabhi nahi sune",
    "ChatGPT ke baare mein 5 baat jo koi nahi batata",
    "2026 ke sabse viral meme ki asli kahani",
    "GTA 6 Lucia ki poori kahani",
    "5 Indian inventions jinhone duniya badal di",
]


# ─── 1. SCRIPT GENERATION (Claude API) ────────────────────────────────────────
def generate_script(topic: str, language: str = "english") -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    lang_instruction = (
        "Write in simple, clear Hindi (Devanagari script is fine but Roman Hindi also OK)."
        if language == "hindi"
        else "Write in simple, punchy English."
    )

    prompt = f"""You are a scriptwriter for a faceless YouTube channel like SG Graphy.
Write a 3-minute narration script about: {topic}

Rules:
- {lang_instruction}
- Hook in first 10 seconds (start with a surprising fact or question)
- 5-7 key points, each 15-20 seconds of speech
- End with: "Subscribe and hit the bell for daily facts!"
- NO stage directions, NO [MUSIC], NO visual cues — just the spoken words
- Keep it conversational and exciting
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


# ─── 2. VOICEOVER (ElevenLabs) ────────────────────────────────────────────────
def generate_voiceover(text: str, output_path: Path, language: str = "english") -> Path:
    voice_id = VOICE_HI if language == "hindi" else VOICE_EN

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY,
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }

    r = requests.post(url, json=payload, headers=headers, stream=True)
    r.raise_for_status()

    with open(output_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=4096):
            if chunk:
                f.write(chunk)

    print(f"  ✅ Voiceover saved: {output_path}")
    return output_path


# ─── 3. STOCK FOOTAGE (Pixabay) ───────────────────────────────────────────────
def get_stock_videos(query: str, count: int = 6) -> list[str]:
    url = "https://pixabay.com/api/videos/"
    params = {
        "key": PIXABAY_API_KEY,
        "q": query,
        "per_page": count,
        "video_type": "film",
        "orientation": "vertical",
        "safesearch": "true",
        "order": "popular",
    }
    r = requests.get(url, params=params)
    r.raise_for_status()
    hits = r.json().get("hits", [])

    urls = []
    for hit in hits:
        videos = hit.get("videos", {})
        for quality in ("medium", "small", "large"):
            v = videos.get(quality, {})
            if v.get("url"):
                urls.append(v["url"])
                break

    # Fallback to horizontal if vertical returns nothing
    if not urls:
        params.pop("orientation")
        r = requests.get(url, params=params)
        for hit in r.json().get("hits", []):
            videos = hit.get("videos", {})
            for quality in ("medium", "small"):
                v = videos.get(quality, {})
                if v.get("url"):
                    urls.append(v["url"])
                    break

    return urls[:count]


def download_file(url: str, dest: Path) -> Path:
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return dest


# ─── 4. VIDEO ASSEMBLY (FFmpeg) ────────────────────────────────────────────────
def get_audio_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True
    )
    return float(result.stdout.strip())


def assemble_video(audio_path: Path, clip_paths: list[Path],
                   output_path: Path, title: str) -> Path:
    tmp = output_path.parent

    # Write concat list
    concat_file = tmp / "concat.txt"
    with open(concat_file, "w") as f:
        for cp in clip_paths:
            f.write(f"file '{cp.resolve()}'\n")

    # Concatenate clips
    bg = tmp / "background.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(concat_file), "-c", "copy", str(bg)],
        check=True, capture_output=True
    )

    duration = get_audio_duration(audio_path)

    # Safe title for drawtext (remove quotes/colons)
    safe_title = title.replace("'", "").replace('"', '').replace(":", " –")[:50]

    # Build final video: scale to 1080x1920, loop bg, overlay audio + title
    subprocess.run([
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(bg),
        "-i", str(audio_path),
        "-t", str(duration),
        "-vf", (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,"
            f"drawtext=text='{safe_title}':"
            "fontcolor=white:fontsize=36:x=(w-text_w)/2:y=80:"
            "box=1:boxcolor=black@0.6:boxborderw=12,"
            "drawtext=text='BharatFacts':"
            "fontcolor=yellow:fontsize=28:x=(w-text_w)/2:y=h-70:"
            "box=1:boxcolor=black@0.5:boxborderw=8"
        ),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(output_path)
    ], check=True, capture_output=True)

    print(f"  ✅ Video assembled: {output_path}")
    return output_path


# ─── 5. YOUTUBE UPLOAD ────────────────────────────────────────────────────────
def upload_to_youtube(video_path: Path, title: str, description: str,
                      oauth_token: str) -> str:
    """
    Upload video to YouTube using OAuth token.
    OAuth token must be obtained first via: python get_oauth_token.py
    Store the refresh token as YOUTUBE_OAUTH_TOKEN in GitHub Secrets.
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2.credentials import Credentials

    creds = Credentials(token=oauth_token)
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": ["facts", "BharatFacts", "viral", "shorts", "GTA6"],
            "categoryId": "27",  # Education
        },
        "status": {"privacyStatus": "public"},
    }

    media = MediaFileUpload(str(video_path), mimetype="video/mp4",
                            resumable=True, chunksize=1024 * 1024)
    request = youtube.videos().insert(part=",".join(body.keys()),
                                       body=body, media_body=media)

    response = None
    while response is None:
        _, response = request.next_chunk()

    video_id = response["id"]
    print(f"  ✅ Uploaded: https://youtube.com/watch?v={video_id}")
    return video_id


# ─── MAIN PIPELINE ────────────────────────────────────────────────────────────
def run(language: str = "english") -> dict:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path("output") / ts
    out.mkdir(parents=True, exist_ok=True)

    topics = TOPICS_HI if language == "hindi" else TOPICS_EN
    topic = random.choice(topics)
    print(f"\n🎬 Topic  : {topic}")
    print(f"🌐 Language: {language}\n")

    # 1. Script
    print("1/4 Writing script...")
    script = generate_script(topic, language)
    (out / "script.txt").write_text(script, encoding="utf-8")

    # 2. Voiceover
    print("2/4 Generating voiceover...")
    audio = out / "voiceover.mp3"
    generate_voiceover(script, audio, language)

    # 3. Stock footage
    print("3/4 Downloading stock footage...")
    keywords = " ".join(topic.split()[:4])
    video_urls = get_stock_videos(keywords, count=6)
    if not video_urls:
        video_urls = get_stock_videos("technology city", count=6)

    clips = []
    for i, url in enumerate(video_urls[:4]):
        dest = out / f"clip_{i}.mp4"
        download_file(url, dest)
        clips.append(dest)
        print(f"   Downloaded clip {i+1}")

    # 4. Assemble
    print("4/4 Assembling video...")
    final = out / "final_video.mp4"
    assemble_video(audio, clips, final, topic)

    # 5. Upload (only if OAuth token available)
    oauth_token = os.environ.get("YOUTUBE_OAUTH_TOKEN")
    video_id = None
    if oauth_token:
        print("5/5 Uploading to YouTube...")
        desc = f"{topic}\n\n{script[:300]}...\n\n#BharatFacts #Shorts #Facts"
        video_id = upload_to_youtube(final, topic, desc, oauth_token)
    else:
        print("ℹ️  No YOUTUBE_OAUTH_TOKEN found — video saved locally only.")
        print(f"   Upload manually: {final}")

    return {
        "topic": topic,
        "language": language,
        "video": str(final),
        "script": str(out / "script.txt"),
        "youtube_id": video_id,
    }


if __name__ == "__main__":
    import sys
    lang = sys.argv[1] if len(sys.argv) > 1 else "english"
    result = run(lang)
    print("\n── RESULT ──────────────────────────────────")
    print(json.dumps(result, indent=2))
