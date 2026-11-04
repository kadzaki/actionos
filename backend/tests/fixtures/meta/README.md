# Recorded Meta responses

These JSON files contain actual recorded provider output; unit-test replay makes
no cloud requests. They are not independent human annotations or scores.

- `muse-program-compile.json`: approved original A19 text-only compile on generated source metadata.
- `muse-program-invalid-framing.json`: a later actual response rejected for full-frame framing with nonempty entity IDs. Retained unchanged; it is not repaired into success.
- `muse-program-compile-v2.json`: actual response to compiler prompt version 2, structurally valid but requiring two material assumption confirmations. The name refers to the prompt version, not an already approved program version.
- `muse-program-screwdriver.json`: actual adapted engagement/turning/withdrawal proposal. Its middle turning anchor exposed the old validator's first-step restriction; validator version 2 accepts the unchanged proposal. Original failure and local revalidation are separate immutable records.
- `observation-prompt-v1.txt`: frozen version-1 prompt prefix from the actual earlier request. Version-2 guidance must not alter old-run request/provenance replay.
- `muse-adapted-observations-w000.json` and `w001.json` (same prefix): actual adapted-program responses, including repeated candidates and clip-boundary uncertainty. They are not final cycle verdicts.
- `muse-adapted-invalid-json-w002.json`: the actual captured reply text with an invalid `combined=1` token. It remains unparsed/unavailable; it is not repaired into a valid observation fixture.
- `muse-step-observations-w000.json` through `muse-step-observations-w004.json`: actual responses on five 12-second, 30fps, 640x360 windows of the user-provided 56.16-second screwdriver recording. w003 is the separately approved retry, not the original failed attempt.

Video source SHA-256:
`617e732c182eb2451a0d3809ff65e68194bf7d228d93b4aa1ba2f5416e306d32`.
The source video is not embedded in these fixtures. The user identifies it as
royalty-free Pexels stock footage; its individual source page is unknown.

The observation recordings preserve provider uncertainty and semantic errors.
w000's placement hypothesis describes holding an already positioned piece and
admits the placement onset was not observed. It does not prove placement.
The original fourth attempt returned HTTP 500 and stopped that batch. A separately
authorized continuation succeeded for w003-w004, then w005 returned unparseable
structured output. Its text was not captured by the prior adapter and is not
fabricated as a fixture. Future attempts now capture text before JSON parsing.
No complete placement/tool cycle, final temporal verdict or full primary-demo
qualification is established by this data.
