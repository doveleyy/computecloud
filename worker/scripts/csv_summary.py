import argparse
import csv
import json
from pathlib import Path


def summarize(input_path: Path) -> dict[str, int | list[str]]:
    with input_path.open("r", encoding="utf-8-sig", newline="") as dataset:
        reader = csv.reader(dataset)
        try:
            columns = next(reader)
        except StopIteration:
            columns = []
        if len(columns) > 1000:
            raise ValueError("CSV has more than 1000 columns")
        if any(len(column) > 200 for column in columns):
            raise ValueError("CSV column name exceeds 200 characters")
        rows = sum(1 for _ in reader)
    return {"rows": rows, "columns": columns}


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize a CSV dataset")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = summarize(args.input)
    args.output.write_text(json.dumps(summary) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
