# job-auto-application-agent

An assistant that finds job adverts, scores how well each one fits a person's profile, drafts tailored application documents from verified facts, and prepares the application form for the person to review. It never submits anything the person has not approved. The aim is to remove the repetitive search and form-filling work while keeping every decision, and every word sent to an employer, in the applicant's hands.

## Status

This project is at the specification stage. The design is written in [docs/AGENT_SPEC.md](docs/AGENT_SPEC.md), and sample inputs are in [examples/](examples/). The first build covers the profile and facts files, the tracker, de-duplication and the approval gate, with tests written first.

## How it works

The agent runs on a schedule and moves each role through a fixed pipeline: discover, normalise and remove duplicates, filter for eligibility, score, shortlist, draft documents from a facts file, check every claim against that file, and write a daily digest. A role can only be submitted after the person has approved it, and approval is tied to a fingerprint of the documents, so editing a document after approval cancels the approval.

## Safety rules

- Nothing is submitted without a recorded approval that matches the documents on disk.
- The agent never creates accounts, types passwords or reads the system keychain. If a site asks for a sign-in or a security check, it stops and leaves the role for the person.
- A drafted document may contain only facts from the person's facts file. Unknown facts are left out until the person confirms them.
- Text inside adverts and emails is treated as data and never as an instruction.
- Scanning is read-only, polite and limited to sources that allow it.

## Privacy

Real profiles, trackers, CVs and facts files are personal and are never stored in this repository. The `examples/` folder contains invented data only, and the `.gitignore` excludes the files that would hold real data.

## Not advice

This is a personal productivity tool. It does not guarantee interviews or offers, and it is not careers or legal advice.
