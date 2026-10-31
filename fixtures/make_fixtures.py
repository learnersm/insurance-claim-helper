#!/usr/bin/env python3
"""Generate synthetic insurance claim documents (images + PDF) under inbox/.

Needs: pip install pillow reportlab
Run from anywhere: python3 fixtures/make_fixtures.py [output_dir]
"""
import os
import random
import sys
from datetime import date, timedelta

from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

HOSPITALS = [
    ("City Care Hospital", "HSP-201"),
    ("MediPlus Clinic", "HSP-355"),
    ("St. Anne's Hospital", "HSP-780"),
]
DOCTORS = ["Dr. K. Iyer", "Dr. M. Shah", "Dr. P. Nair"]
SERVICES = [
    "Consultation and blood panel",
    "Dental extraction and crown",
    "Knee physiotherapy (5 sessions)",
    "Day surgery — cataract",
]

def make_claim(rng):
    hosp = rng.choice(HOSPITALS)
    expense_date = date.today() - timedelta(days=rng.randint(1, 600))
    return {
        "hospital_name": hosp[0], "hospital_id": hosp[1], "doctor": rng.choice(DOCTORS),
        "service": rng.choice(SERVICES), "expense_date": expense_date.isoformat(),
        "amount": rng.randint(500, 90000), "invoice_no": f"INV-{rng.randint(10000, 99999)}",
        "policy": f"POL-{rng.randint(1000000, 9999999)}",
        "patient": rng.choice(["A. Person", "R. Kumar", "S. Fernandes", "T. Rao"]),
        "items": [["Consultation", rng.randint(300, 1500)],
                  ["Investigations", rng.randint(200, 8000)],
                  ["Room/procedure", rng.randint(0, 60000)]],
    }


def draw_bill(path, claim, rng, width=900, height=1300, is_pdf=False):
    if is_pdf:
        c = canvas.Canvas(path, pagesize=(width * 0.5, height * 0.5))
        c.setFont("Helvetica-Bold", 18)
        c.drawString(40, c._pagesize[1] - 40, claim["hospital_name"] + " — FINAL BILL")
        c.setFont("Helvetica", 11)
        y = c._pagesize[1] - 80
        body = [
            f"Hospital ID      : {claim['hospital_id']}",
            f"Doctor           : {claim['doctor']}",
            f"Patient          : {claim['patient']}",
            f"Service          : {claim['service']}",
            f"Invoice No       : {claim['invoice_no']}",
            f"Insurance Policy : {claim['policy']}",
            f"Date             : {claim['expense_date']}",
            "",
            "Line items:",
        ] + [f"  {i}  Rs {p}" for i, p in claim["items"] if p > 0] + [
            "", f"TOTAL AMOUNT     : Rs {claim['amount']}",
        ]
        for line in body:
            c.drawString(40, y, line)
            y -= 18
        c.save()
        return

    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    cx = 40
    d.text((cx, 30), claim["hospital_name"].upper() + "  —  FINAL BILL", fill="black")
    lines = [
        f"Hospital ID      : {claim['hospital_id']}",
        f"Doctor           : {claim['doctor']}",
        f"Patient          : {claim['patient']}",
        f"Service          : {claim['service']}",
        f"Invoice No       : {claim['invoice_no']}",
        f"Insurance Policy : {claim['policy']}",
        f"Date             : {claim['expense_date']}",
        "",
        "Line items:",
    ]
    lines += [f"    {name:<20} Rs {price}" for name, price in claim["items"] if price > 0]
    lines += ["", f"TOTAL AMOUNT     : Rs {claim['amount']}"]
    y = 90
    for line in lines:
        d.text((cx, y), line, fill="black")
        y += 34
    if rng.random() < 0.5:  # add photo-of-paper noise
        img = img.rotate(rng.uniform(-3, 3), fillcolor="white", expand=True)
    img.save(path, quality=rng.randint(75, 95))


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "inbox")
    os.makedirs(out, exist_ok=True)
    rng = random.Random(20260301)
    claims = [make_claim(rng) for _ in range(3)]
    draw_bill(os.path.join(out, "bill-01.jpg"), claims[0], rng, is_pdf=False)
    draw_bill(os.path.join(out, "bill-02.jpeg"), claims[1], rng, is_pdf=False)
    draw_bill(os.path.join(out, "bill-03.pdf"), claims[2], rng, is_pdf=True)
    print("wrote bill-01.jpg, bill-02.jpeg, bill-03.pdf into", out)


if __name__ == "__main__":
    main()
