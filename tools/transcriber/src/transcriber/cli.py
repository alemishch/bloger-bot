import glob
import json
import os
import click
from transcriber.config import settings
from transcriber.whisper_transcriber import WhisperTranscriber


@click.group()
def cli():
    pass


@cli.command()
@click.argument("input_path", type=click.Path(exists=True))
@click.option("--output-dir", default=None)
def transcribe(input_path: str, output_dir: str | None):
    output_dir = output_dir or settings.TRANSCRIPTION_OUTPUT_DIR
    t = WhisperTranscriber()
    result = t.transcribe(input_path)
    path = t.save(result, output_dir)
    print(f"Saved: {path}")


@cli.command()
@click.option("--input-dir", default=None)
@click.option("--output-dir", default=None)
def batch(input_dir: str | None, output_dir: str | None):
    input_dir = input_dir or settings.DOWNLOAD_DIR
    output_dir = output_dir or settings.TRANSCRIPTION_OUTPUT_DIR

    t = WhisperTranscriber()
    for job_file in glob.glob(os.path.join(input_dir, "job_*.json")):
        with open(job_file, "r", encoding="utf-8") as f:
            job = json.load(f)
        if job.get("status") != "downloaded":
            continue
        file_path = job["file_path"]
        if not os.path.exists(file_path):
            continue

        result = t.transcribe(file_path)
        transcript_path = t.save(result, output_dir)
        job["status"] = "transcribed"
        job["transcript_path"] = transcript_path
        with open(job_file, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=2)
        print(f"Transcribed: {file_path}")


if __name__ == "__main__":
    cli()