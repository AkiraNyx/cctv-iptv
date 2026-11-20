"""Validate the APTV playlist and collect independently measured cloud samples."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fractions import Fraction
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qsl, urljoin, urlsplit
import argparse
import json
import re
import socket
import subprocess
import unicodedata


CHANNELS = {f"CCTV-{i}" for i in range(1, 18)} | {"CCTV-5+", "CCTV-4K", "CCTV-16-4K"}
FEEDS = {
    "best-fan": "https://raw.githubusercontent.com/best-fan/iptv-sources/main/cn_cctv.m3u8",
    "iptv-org": "https://iptv-org.github.io/iptv/countries/cn.m3u",
}
MIB = 1024 * 1024


def identify(name):
    name = unicodedata.normalize("NFKC", name).strip().upper()
    match = re.match(r"CCTV[\s_-]*(4\s*K|1[0-7]|[1-9])\s*(?![\dK])(\+)?", name)
    if not match or re.search(r"欧洲|美洲|港澳|EUROPE|AMERICA|HONG KONG|MACAU", name):
        return None
    number = match[1].replace(" ", "")
    if number == "16" and re.search(r"4\s*K", name[match.end():]):
        return "CCTV-16-4K"
    key = f"CCTV-{number}{match[2] or ''}"
    return key if key in CHANNELS else None


def http_url(url):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
        raise ValueError("expected an HTTP(S) URL without user credentials")
    return parts


def playlist(text, strict=False):
    lines = text.removeprefix("\ufeff").splitlines()
    if not lines or lines[0].split(maxsplit=1)[:1] != ["#EXTM3U"]:
        raise ValueError("missing #EXTM3U header")
    name = None
    for line in map(str.strip, lines[1:]):
        if line.startswith("#EXTINF:"):
            if strict and name is not None:
                raise ValueError("missing URL for " + name)
            match = re.fullmatch(r'#EXTINF:(?:[^",]|"[^"]*")*,(.+)', line)
            name = match[1] if match else ""
            if strict and (not name or not identify(name)):
                raise ValueError("invalid channel label: " + line)
        elif line and not line.startswith("#"):
            if strict and name is None:
                raise ValueError("URL without an #EXTINF entry")
            if name is not None and (key := identify(name)):
                if strict:
                    http_url(line)
                yield key, line
            name = None
    if strict and name is not None:
        raise ValueError("missing final URL")


def read_baseline():
    text = Path("cctv.m3u").read_text(encoding="utf-8")
    entries = list(playlist(text, strict=True))
    if len(entries) != len(CHANNELS) or {key for key, _ in entries} != CHANNELS:
        raise ValueError("cctv.m3u must contain exactly one entry for each of the 20 channels")
    if len({url for _, url in entries}) != len(CHANNELS):
        raise ValueError("cctv.m3u contains a repeated URL")
    digest = sha256(text.encode("utf-8")).hexdigest()
    baseline = json.loads(Path("baseline.json").read_text(encoding="utf-8"))
    if baseline["playlist_sha256"] != digest:
        raise ValueError("baseline.json and cctv.m3u describe different snapshots")
    if set(baseline["channels"]) != CHANNELS:
        raise ValueError("baseline.json must describe all 20 channels")
    return entries, baseline, digest


def fetch(url, path, size_limit, timeout=12):
    for _ in range(4):
        target = http_url(url)
        port = target.port or (443 if target.scheme == "https" else 80)
        addresses = {ip_address(info[4][0]) for info in socket.getaddrinfo(target.hostname, port, type=socket.SOCK_STREAM)}
        public = sorted((ip for ip in addresses if ip.is_global and not ip.is_multicast), key=lambda ip: (ip.version, int(ip)))
        if not public:
            raise ValueError("no public IP address")
        resolve = []
        try:
            ip_address(target.hostname)
        except ValueError:
            pinned = ",".join(f"[{ip}]" if ip.version == 6 else str(ip) for ip in public[:4])
            resolve = ["--resolve", f"{target.hostname}:{port}:{pinned}"]
        result = subprocess.run(
            ["curl", "--silent", "--noproxy", "*", "--proto", "=http,https", "--connect-timeout", "4",
             "--max-time", str(timeout), "--max-filesize", str(size_limit), "--output", str(path),
             *resolve, "--write-out", "%{http_code}\n%{redirect_url}\n%{remote_ip}", "--", url],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        status, redirect, remote = result.stdout.split("\n", 2)
        if result.returncode:
            raise ValueError(f"download failed (curl {result.returncode}, HTTP {status})")
        if redirect and status in {"301", "302", "303", "307", "308"}:
            url = urljoin(url, redirect)
            continue
        if status != "200":
            raise ValueError(f"HTTP {status}")
        return url, ip_address(remote).version
    raise ValueError("too many redirects")


def sample_hls(url, directory):
    manifest = directory / "remote.m3u8"
    for _ in range(3):
        url, family = fetch(url, manifest, 2 * MIB)
        lines = [line.strip() for line in manifest.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        if not lines or lines[0] != "#EXTM3U":
            raise ValueError("response is not an HLS manifest")
        variants = []
        for tag, uri in zip(lines, lines[1:]):
            if tag.startswith("#EXT-X-STREAM-INF:") and not uri.startswith("#"):
                bandwidth = re.search(r"(?:^|[:,])BANDWIDTH=(\d+)", tag)
                variants.append((int(bandwidth[1]) if bandwidth else 0, uri))
        if not variants:
            break
        url = urljoin(url, max(variants)[1])
    else:
        raise ValueError("too many nested HLS master playlists")

    # shortcut: one unencrypted HLS rendition, add support when another format is needed.
    if any(line.startswith(("#EXT-X-KEY:", "#EXT-X-BYTERANGE:", "#EXT-X-GAP")) for line in lines):
        raise ValueError("encrypted, byte-range, or gap-tagged HLS")
    init = [line for line in lines if line.startswith("#EXT-X-MAP:")]
    if len(set(init)) > 1 or any("BYTERANGE" in line for line in init):
        raise ValueError("multiple or byte-range HLS initialization sections")
    tail = lines[max((i + 1 for i, line in enumerate(lines) if line == "#EXT-X-DISCONTINUITY"), default=0):]
    segments = []
    duration = None
    for line in tail:
        if line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif duration is not None and not line.startswith("#"):
            segments.append((duration, line))
            duration = None
    selected = segments[-2:]
    if not selected or any(not 0 < seconds <= 30 for seconds, _ in selected):
        raise ValueError("no bounded HLS media segment sample")

    local = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:30"]
    families = {family}
    if init:
        match = re.search(r'URI="([^"]+)"', init[0])
        if not match:
            raise ValueError("missing HLS initialization URL")
        _, family = fetch(urljoin(url, match[1]), directory / "init.mp4", 4 * MIB)
        families.add(family)
        local.append('#EXT-X-MAP:URI="init.mp4"')
    for index, (seconds, uri) in enumerate(selected):
        filename = f"segment-{index}.{'m4s' if init else 'ts'}"
        _, family = fetch(urljoin(url, uri), directory / filename, 48 * MIB)
        families.add(family)
        local.extend((f"#EXTINF:{seconds},", filename))
    path = directory / "sample.m3u8"
    path.write_text("\n".join(local + ["#EXT-X-ENDLIST", ""]), encoding="utf-8")
    return path, sorted(families), sum(seconds for seconds, _ in selected)


def measure(path, expected):
    media = ["-protocol_whitelist", "file", "-i", str(path)]
    inspected = subprocess.run(
        ["ffprobe", "-v", "error", *media, "-select_streams", "v:0", "-show_streams", "-show_packets",
         "-show_entries", "stream=width,height,codec_name,pix_fmt,field_order,r_frame_rate,avg_frame_rate,color_transfer,color_primaries:packet=pts_time,duration_time,size",
         "-of", "json"], capture_output=True, text=True, timeout=15,
    )
    if inspected.returncode:
        raise ValueError("FFprobe could not read the video sample")
    data = json.loads(inspected.stdout)
    if not data.get("streams") or not data.get("packets"):
        raise ValueError("no video packets")
    video = data["streams"][0]
    packets = [p for p in data["packets"] if "pts_time" in p]
    if not packets:
        raise ValueError("video packets lack presentation timestamps")
    duration = max(float(p["pts_time"]) + float(p.get("duration_time", 0)) for p in packets) - min(float(p["pts_time"]) for p in packets)
    if not expected * 0.75 <= duration <= expected + 2:
        raise ValueError("video sample duration disagrees with HLS manifest")

    decoded = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-threads", "2", *media, "-map", "0:v:0",
         "-progress", "pipe:1", "-f", "null", "-"], capture_output=True, text=True, timeout=40,
    )
    fields = dict(line.split("=", 1) for line in decoded.stdout.splitlines() if "=" in line)
    if decoded.returncode or int(fields.get("frame", 0)) == 0:
        raise ValueError("FFmpeg strict video decode failed")
    fps = next((float(Fraction(video[key])) for key in ("avg_frame_rate", "r_frame_rate")
                if video.get(key) not in (None, "0/0", "0/1")), 0)
    return {
        "width": video["width"], "height": video["height"], "codec": video["codec_name"], "fps": round(fps, 2),
        "pixel_format": video.get("pix_fmt"), "field_order": video.get("field_order"),
        "color_transfer": video.get("color_transfer"), "color_primaries": video.get("color_primaries"),
        "video_mbps": round(sum(int(p["size"]) for p in packets) * 8 / duration / 1e6, 2),
        "sample_seconds": round(duration, 2), "decoded_frames": int(fields["frame"]),
    }


def quality(video):
    hdr = video.get("color_transfer") in {"arib-std-b67", "smpte2084"} and video.get("color_primaries") == "bt2020"
    interlaced = video.get("field_order") in {"tt", "bb", "tb", "bt"}
    return video["height"], hdr, video["fps"] * (2 if interlaced else 1), video["video_mbps"]


def probe(job):
    channel, source, url = job
    result = {"channel": channel, "source": source, "url": url}
    try:
        with TemporaryDirectory() as temp:
            sample, families, seconds = sample_hls(url, Path(temp))
            video = measure(sample, seconds)
        if channel.endswith("4K") and (video["width"] < 3840 or video["height"] < 2160):
            result.update(status="4k_label_mismatch", video=video, ip_versions=families)
        else:
            result.update(status="video_decoded", video=video, ip_versions=families)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        result.update(status="unverified", reason=str(error).split("\n", 1)[0][:160])
        if isinstance(error, subprocess.TimeoutExpired):
            result["reason"] = "sample timed out"
    return result


def audit(entries, baseline, digest):
    jobs = [(channel, "fujian_snapshot", url) for channel, url in entries]
    seen = {url for _, url in entries}
    feeds = {}
    with TemporaryDirectory() as temp:
        path = Path(temp) / "feed.m3u"
        for name, url in FEEDS.items():
            added = {}
            try:
                fetch(url, path, 3 * MIB)
                for channel, stream in playlist(path.read_text(encoding="utf-8-sig")):
                    if stream in seen or added.get(channel, 0) >= 3:
                        continue
                    try:
                        target = http_url(stream)
                        if any(re.search(r"token|signature|authinfo|auth_key|password", key, re.I)
                               for key, _ in parse_qsl(target.query)):
                            continue
                    except ValueError:
                        continue
                    jobs.append((channel, name, stream))
                    seen.add(stream)
                    added[channel] = added.get(channel, 0) + 1
                feeds[name] = {"status": "downloaded", "selected": sum(added.values())}
            except (ValueError, OSError, subprocess.TimeoutExpired):
                feeds[name] = {"status": "download_failed"}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(probe, jobs))
    for entry in results:
        if entry["source"] != "fujian_snapshot" and entry["status"] == "video_decoded":
            entry["higher_than_snapshot"] = quality(entry["video"]) > quality(baseline["channels"][entry["channel"]])
    results.sort(key=lambda entry: (
        entry["channel"], entry["source"] == "fujian_snapshot", entry["status"] == "video_decoded",
        quality(entry["video"]) if "video" in entry else (0, False, 0, 0),
    ), reverse=True)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "network": "GitHub-hosted runner",
        "fujian_playback_reverified": False,
        "playlist_sha256": digest,
        "snapshot_verified_at": baseline["verified_at"],
        "feeds": feeds, "results": results,
    }


if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("--check", action="store_true", help="validate only, without network access")
    cli.add_argument("--output", default="status.json", type=Path)
    args = cli.parse_args()
    entries, baseline, digest = read_baseline()
    print(f"Validated {len(entries)} channels", flush=True)
    if not args.check:
        status = audit(entries, baseline, digest)
        args.output.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Cloud probes: {sum(r['status'] == 'video_decoded' for r in status['results'])}/{len(status['results'])} decoded")
