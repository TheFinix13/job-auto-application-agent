# job-auto-application-agent

An assistant that finds job adverts, scores how well each one fits a person's profile, drafts tailored application documents from verified facts, and prepares the application form for the person to review. It never submits anything the person has not approved. The aim is to remove the repetitive search and form-filling work while keeping every decision, and every word sent to an employer, in the applicant's hands.

## Status

The design is written in [docs/AGENT_SPEC.md](docs/AGENT_SPEC.md), and sample inputs are in [examples/](examples/). The first four build steps are in `src/job_agent/`: the facts file and fact checker, the profile, the tracker, de-duplication, and the approval gate, with 267 tests (`pip install -e .[dev]` then `pytest`). Discovery, tailoring and the form filler are not built yet.

The approval gate is the only place a submit can happen, and it refuses unless every check passes. The gate hashes the CV, the letter and the form answers together and submits exactly the bytes it hashed. The approve command needs a real terminal and a typed confirmation of company and role. Known limits are stated rather than hidden: a terminal and typed words are not authentication, and a program that can open a pseudo-terminal could pass them, so the checks make approval a deliberate, recorded act and not a proof of identity.

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
