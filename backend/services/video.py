import os
import requests


def download_video(video_url, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    response = requests.get(
        video_url,
        stream=True,
        timeout=60,
        headers={
            "User-Agent": "Mozilla/5.0"
        }
    )
    response.raise_for_status()

    with open(output_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)

    return output_path