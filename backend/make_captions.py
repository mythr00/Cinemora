import whisper
from pathlib import Path


INPUT_AUDIO = Path(r"media\test-project-2\narration.wav")
OUTPUT_SRT = Path(r"media\test-project-2\captions.srt")


def format_timestamp(seconds):
    milliseconds = int(round(seconds * 1000))

    hours = milliseconds // 3_600_000
    milliseconds %= 3_600_000

    minutes = milliseconds // 60_000
    milliseconds %= 60_000

    seconds = milliseconds // 1000
    milliseconds %= 1000

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{seconds:02d},"
        f"{milliseconds:03d}"
    )


print("Loading Whisper...")
model = whisper.load_model("base")

print("Transcribing narration...")
result = model.transcribe(
    str(INPUT_AUDIO),
    fp16=False,
)

print("Creating SRT...")

with open(
    OUTPUT_SRT,
    "w",
    encoding="utf-8",
) as file:

    for index, segment in enumerate(
        result["segments"],
        start=1,
    ):

        start = format_timestamp(
            segment["start"]
        )

        end = format_timestamp(
            segment["end"]
        )

        text = segment["text"].strip()

        file.write(
            f"{index}\n"
            f"{start} --> {end}\n"
            f"{text}\n\n"
        )


print()
print("================================")
print("SRT CREATED SUCCESSFULLY")
print("================================")
print(f"Output: {OUTPUT_SRT}")