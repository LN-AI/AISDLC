# Telling the reviewer what not to gate on

Some findings are real and not worth blocking a merge for. A fake key in a fixture. A
directory that is being deleted next quarter. One `eval` in an admin-only CLI that three
people can reach. Before this, saying so meant clicking a waiver in the UI — once per
finding, once per project, invisible to anyone reading the code, and gone the moment
somebody forked the repository.

Now you can say it in the repository, in two ways.

## The one rule to understand first

**An ignore rule lowers the gate. It does not hide the evidence.**

A suppressed finding is still reported on the run, still exported to SARIF (marked
`suppressed`, with your rule as its justification), and still counted. What it loses is its
vote in the gate.

This is deliberate and it is not negotiable, because the alternative is that anyone with
commit access can turn a Red release Green by adding two lines to a file — and the review
would then agree with them, in writing, in an audit trail. If you want a finding *gone*,
that is a waiver, and a waiver is a decision a person signs.

One consequence worth stating plainly: **suppressing every finding does not make a Red
review Green.** The gate is floored by the findings and separately by the reviewer's own
verdict, and an ignore rule cannot talk it below the second one.

## `.lmignore`

One file, at the repository root. It is read at the commit being reviewed, so a rule you
add and a review you run in the same pull request agree with each other.

```
# Blank lines and #-comments are ignored.

legacy/**                        everything under legacy/
fixtures                         any path segment called "fixtures", at any depth
*.generated.ts                   by basename, anywhere in the tree
src/parser.py: SEC-003           that one file, that one control
infra/**: SEC-003, SEC-010       tracked in JIRA-4412
!legacy/payments/**              ...but not this subtree
```

**Patterns.** A documented subset of gitignore's, not all of it:

| | |
|---|---|
| `*` | matches within one path segment — `src/*.py` does **not** cover `src/a/b.py` |
| `**` | matches across segments |
| `?` | one character |
| trailing `/` | that directory and everything under it |
| no `/`, no wildcard | any whole path segment — `vendor` covers `a/vendor/b.py` |
| no `/`, with a wildcard | the basename, anywhere |
| leading `!` | do **not** suppress |

**Last matching rule wins**, so a `!` line placed after a broad rule carves an exception
out of it. Put your negations below the rule they are narrowing.

**Controls.** Anything after the pattern is read as control ids first, then free text. A
token that looks like a control id (`SEC-003`, `CWE-89`, `OWASP-A01`) is one; the first
token that does not starts the reason. A rule with no control applies to every finding in
that path — which is usually wider than you want.

**Reasons are optional and you should write them anyway.** They are not enforced, because
a rule refused over a style requirement is a feature nobody adopts. But the run reports
how many of your rules give no reason, and six months from now that number is the
difference between a decision and a mystery.

## Inline `lm-ignore`

For one line, next to the line.

```python
q = f"SELECT * FROM t WHERE id={uid}"   # lm-ignore: SEC-001 internal admin tool

# lm-ignore-next-line CWE-79 the caller sanitizes this
render(user_input)
```

```javascript
/* lm-ignore-file  vendored, upstream's problem */
```

* `lm-ignore` — findings on **this** line.
* `lm-ignore-next-line` — findings on the **following** line.
* `lm-ignore-file` — findings anywhere in this file. Honoured only in the **first 20
  lines**; further down it is treated as an ordinary line directive, because a directive
  nobody reading the top of the file can see should not silently exempt the whole file.

The comment marker is not parsed. The directive is found anywhere in the line, so `#`,
`//`, `--`, `/* */` and `<!-- -->` all work, in any language, without this platform
carrying a table of comment syntaxes that would be wrong for whichever language it got
wrong.

Add a control id to narrow it. Without one, the directive suppresses every finding on that
line.

## A rule that matched nothing tells you

Every run reports the rules that fired and, more usefully, **the rules that did not**:

> `2 ignore rule(s) matched nothing`
> `legcy/**` (line 4) · `lm-ignore` in `app/x.py` (line 12)

This exists because the way this feature fails is a typo. You write a pattern, you believe
you have suppressed something, the build still fails, and the tool looks broken. An unused
rule on the result is a typo you can see. It is not an error — a rule can legitimately
outlive the code it was about — but it is not doing what whoever wrote it thinks.

A malformed line is reported the same way and does not invalidate the rest of the file. A
review of a repository with one bad line behaves as though that line were absent, not as
though the file were.

## Pull requests

A pull request is a diff, not a file list, so there is nothing to find `.lmignore` *in* —
it is fetched from the repository at the commit under review. Costs one API call and fails
open: a repository without one answers 404, which is most of them.

Inline directives on a pull request are read out of the flagged code itself rather than
from the file, which is slightly looser: an evidence quote can span a few lines, so a
directive on a neighbouring line inside that quote counts where it would not in a full
file. The alternative was that inline suppression silently did nothing on the source type
most reviews actually use.

## Moving your existing waivers into a file

If you have been clicking waivers for months, export them once:

```
python scripts/lmsdlc.py findings --lmignore .lmignore --repo acme/portal
```

or `GET /api/v1/findings/lmignore?repo=acme/portal`.

**Read it before you commit it.** A waiver was granted about *one finding*, matched on its
control, path, title and evidence. A `.lmignore` rule can only express the path and the
control, so every exported rule is **wider** than the waiver it came from — it will also
cover a different finding of the same control in the same file. That is why this hands you
a file to review instead of writing one into your repository.

Two things do not survive the export, both by design:

* **An expiry.** An accepted risk with an end date cannot be expressed in a file with no
  clock, so a lapsed acceptance is left out rather than exported as permanent. That is the
  one thing the expiry exists to prevent.
* **A waiver with no file path** — a design-level finding. It is listed as a `# SKIPPED`
  comment rather than dropped silently, because a waiver that vanishes on export is one
  somebody still has to re-enter by hand.

## What this does not do

* **No per-directory ignore files.** A `.lmignore` in a subdirectory is not read. Treating
  one as repository-wide would be the wrong half of that feature, and doing it properly
  needs scoping rules nobody has asked for yet.
* **No fingerprint-level rules.** You cannot suppress one specific finding and leave
  another of the same control in the same file. That is what a waiver is for.
* **No severity or gate overrides.** A rule removes a finding's vote; it cannot change what
  the vote would have been.
* **500 rules maximum.** Past that the file is suppressing more than it is reviewing, and
  the run says so rather than silently reading half of it.
