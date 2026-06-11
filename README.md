Here it is, ready to paste into the GitHub README editor:

```
# VideoTrim

A self-hosted web app for trimming and merging videos using ffmpeg. Upload a video, set start/end timestamps for one or more cuts, and get a single merged output — all from the browser.

## Features

- Drag & drop upload up to 10 GB
- Multiple cut segments that auto-merge into one file
- Fast mode (stream copy) or re-encode (frame-accurate)
- Output format: MP4, MKV, MOV, WebM
- Upload and trim progress bars
- Auto-deletes files older than 2 hours

## Deploy with Docker

```bash
git clone https://github.com/Tinmisho/VideoTrim.git
cd VideoTrim
docker compose up -d
```

Open `http://localhost:5000`

## Update

```bash
git pull
docker compose build --no-cache
docker compose up -d
```

## Reverse Proxy (Nginx)

```nginx
server {
    server_name videotrim.yourdomain.com;

    location / {
        proxy_pass http://localhost:5000;
        proxy_read_timeout 600;
        client_max_body_size 10G;
    }
}
```

## Stack

Python · Flask · ffmpeg · Gunicorn · Docker

---

*vibe coded by M.G*
```
