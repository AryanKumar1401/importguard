"""Tables to CSV, TSV, SQLite and spreadsheets (a large library)."""
import csv
import decimal
import email.mime.multipart
import email.mime.text
import json
import sqlite3
import xml.dom.minidom

from tablekit import _formats


def write_csv(rows, path, header=None):
    """Write rows (iterables) to a CSV file at path."""
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        if header:
            writer.writerow(header)
        writer.writerows(rows)
