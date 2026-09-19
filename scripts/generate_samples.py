"""Generate the sample Job Description and sample resumes used for testing.

Writes a deliberate spread of cases so an end-to-end run exercises every path:
PDF / DOCX / TXT parsing, a strong match, a partial match, an off-domain weak
match, a near-empty resume, a duplicate, and an unreadable file.

Run:  python scripts/generate_samples.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLES = ROOT / "samples"
RESUMES = SAMPLES / "resumes"

JOB_DESCRIPTION = """Senior Performance Marketing Manager
Varsity Learning Technologies · Bengaluru (Hybrid)

About the role
We are hiring a Senior Performance Marketing Manager to own paid acquisition for
our online upskilling programs. You will run the full paid funnel across search
and social, own CAC and enrolment targets, and work closely with the content and
product teams.

Key Responsibilities
- Own and scale paid acquisition across Google Ads and Meta Ads with a monthly budget above INR 1 crore.
- Drive down cost per enrolment while growing qualified lead volume quarter on quarter.
- Build and run a structured experimentation roadmap across creatives, landing pages and audiences.
- Partner with the content team on ad copy and landing page messaging for high-consideration programs.
- Report weekly on CAC, ROAS and funnel conversion to the leadership team.
- Manage and mentor a team of two performance marketing executives.

Required Skills and Qualifications
- Must have 5+ years of hands-on performance marketing experience.
- Required: proven expertise running Google Ads and Meta Ads at scale.
- Mandatory: strong command of conversion rate optimization and A/B testing.
- Essential: working knowledge of GA4 and marketing analytics dashboards.
- Required qualification: Bachelor's degree in any discipline.
- Must have experience owning CAC, ROAS and unit economics for a paid funnel.

Preferred / Good to have
- Preferred: experience in edtech or another high-consideration B2C category.
- Good to have: experience with SQL for self-serve funnel analysis.
- Nice to have: exposure to lifecycle and email marketing.
- Bonus: experience managing a small team.
- Advantage: Google Ads certification.

Location
This is a hybrid role based in Bengaluru. Candidates must be willing to work
from the Bengaluru office three days a week.
"""

RESUME_STRONG = """ANJALI MEHTA
anjali.mehta@example.com | +91 98200 12345 | Bengaluru, India
linkedin.com/in/anjalimehta

PROFESSIONAL SUMMARY
Performance marketer with 7 years of experience scaling paid acquisition for
subscription and high-consideration consumer products. Owns CAC and payback
targets end to end.

WORK EXPERIENCE

Senior Performance Marketing Manager | SkillForge Learning | Mar 2021 - Present
- Owned paid acquisition across Google Ads and Meta Ads with a monthly budget of INR 1.4 crore.
- Reduced cost per enrolment by 34% over six quarters while growing qualified leads 2.2x.
- Built a weekly experimentation roadmap covering ad creatives, landing pages and audience cohorts.
- Ran structured A/B testing on landing pages, lifting checkout conversion rate from 3.1% to 5.4%.
- Reported CAC, ROAS and funnel conversion to the leadership team every week using GA4 and Looker.
- Managed and mentored a team of two performance marketing executives.

Performance Marketing Manager | Shopmint | Jun 2018 - Feb 2021
- Ran AdWords search, shopping and paid social campaigns for a D2C marketplace.
- Used SQL to build self-serve funnel dashboards for the growth team.
- Partnered with content on ad copy and landing page messaging for premium categories.

EDUCATION
MBA, Marketing - Symbiosis Institute of Business Management, 2018
B.Com - University of Mumbai, 2016

SKILLS
Google Ads, Meta Ads, GA4, Looker, SQL, A/B testing, conversion rate optimization,
landing page optimization, CAC and ROAS modelling, HubSpot, lifecycle marketing

CERTIFICATIONS
Google Ads Search Certification, 2023
Google Analytics 4 Certification, 2022

Notice Period: 30 days
"""

RESUME_PARTIAL = """Rahul Nair
rahul.nair@example.com | +91 99456 78901 | Pune

SUMMARY
Digital marketing professional with 4 years of experience across paid and
organic channels for B2B software companies.

EXPERIENCE

Digital Marketing Specialist | CloudLedger Systems | Aug 2021 - Present
- Managed paid acquisition for enterprise SaaS products across search and social.
- Grew inbound demo requests 60% year on year through paid search and content.
- Built monthly reporting on pipeline contribution and cost per lead.

Marketing Executive | Brightforms | Jul 2020 - Jul 2021
- Supported SEO and email marketing campaigns.
- Assisted with landing page copy and creative briefs.

EDUCATION
B.Tech, Computer Science - Pune Institute of Technology, 2020

SKILLS
Google Ads, LinkedIn Ads, SEO, HubSpot, email marketing, Google Analytics

Notice Period: 60 days
"""

RESUME_WEAK = """Vikram Desai
vikram.desai@example.com | +91 90000 11223 | Hyderabad

PROFILE
Backend engineer with 6 years of experience building distributed systems.

WORK EXPERIENCE

Senior Software Engineer | Nimbus Data Systems | Jan 2020 - Present
- Designed and built microservices in Python and Go serving 40M requests per day.
- Migrated the billing platform to Kubernetes, cutting infrastructure cost by 28%.
- Mentored four junior engineers and owned the on-call rotation.

Software Engineer | Trailhead Tech | Jul 2018 - Dec 2019
- Developed REST APIs and CI/CD pipelines using Jenkins and Docker.

EDUCATION
B.Tech, Information Technology - NIT Warangal, 2018

SKILLS
Python, Go, Kubernetes, Docker, PostgreSQL, AWS, REST APIs, CI/CD
"""

RESUME_SPARSE = """Meera Iyer
meera.iyer@example.com

Marketing professional.
Looking for new opportunities.
"""

RESUME_NO_NAME = """Curriculum Vitae

PROFILE
Growth marketing lead with 8 years of experience in paid acquisition for
consumer subscription businesses.

EXPERIENCE
Growth Marketing Lead | Feb 2019 - Present
- Owned paid media across Google Ads, Meta Ads and programmatic display.
- Scaled monthly ad spend from INR 20 lakh to INR 1.1 crore while holding CAC flat.
- Led conversion rate optimization and a continuous A/B testing programme on landing pages.
- Reported ROAS and unit economics to the founders every week.
- Used GA4 and SQL for funnel analysis and cohort reporting.

Performance Marketing Manager | Mar 2016 - Jan 2019
- Ran paid search and paid social for an edtech platform selling professional certification courses.

EDUCATION
Bachelor of Business Administration, 2015

SKILLS
Google Ads, Meta Ads, GA4, SQL, CRO, A/B testing, programmatic, lifecycle marketing
"""


def write_txt(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    print(f"  wrote {path.relative_to(ROOT)}")


def write_pdf(path: Path, text: str) -> None:
    try:
        import pymupdf
    except ImportError:  # pragma: no cover
        import fitz as pymupdf

    doc = pymupdf.open()
    lines = text.split("\n")
    # ~52 lines per A4 page at 11pt
    for start in range(0, len(lines), 52):
        page = doc.new_page()
        chunk = "\n".join(lines[start:start + 52])
        page.insert_textbox(pymupdf.Rect(56, 56, 540, 780), chunk, fontsize=10.5, fontname="helv")
    doc.save(str(path))
    doc.close()
    print(f"  wrote {path.relative_to(ROOT)} ({len(lines) // 52 + 1} page(s))")


def write_docx(path: Path, text: str) -> None:
    import docx

    document = docx.Document()
    for line in text.split("\n"):
        document.add_paragraph(line)
    document.save(str(path))
    print(f"  wrote {path.relative_to(ROOT)}")


def main() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    RESUMES.mkdir(parents=True, exist_ok=True)

    print("Job Description:")
    write_txt(SAMPLES / "job_description.txt", JOB_DESCRIPTION)
    write_pdf(SAMPLES / "job_description.pdf", JOB_DESCRIPTION)

    print("Resumes:")
    write_pdf(RESUMES / "anjali_mehta_resume.pdf", RESUME_STRONG)
    write_docx(RESUMES / "rahul_nair_resume.docx", RESUME_PARTIAL)
    write_pdf(RESUMES / "vikram_desai_resume.pdf", RESUME_WEAK)
    write_txt(RESUMES / "meera_iyer_resume.txt", RESUME_SPARSE)
    write_docx(RESUMES / "unnamed_candidate_resume.docx", RESUME_NO_NAME)
    # Same content as anjali_mehta_resume.pdf in a different format -> duplicate detection.
    write_docx(RESUMES / "anjali_mehta_resume_copy.docx", RESUME_STRONG)

    print("Edge cases:")
    corrupt = RESUMES / "corrupted_resume.pdf"
    corrupt.write_bytes(b"%PDF-1.4\nthis file is deliberately truncated and invalid")
    print(f"  wrote {corrupt.relative_to(ROOT)}")
    empty = RESUMES / "empty_resume.txt"
    empty.write_text("", encoding="utf-8")
    print(f"  wrote {empty.relative_to(ROOT)}")

    print("\nDone. Sample files are in samples/.")


if __name__ == "__main__":
    main()
