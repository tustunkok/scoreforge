# NOTICE

ScoreForge bundles and depends on third-party components with their own
licences. This file records what we use and under which terms.

## Bundled / invoked components

| Component | Role | Licence |
|---|---|---|
| [DuET](https://github.com/wei-zeng98/DuET) (code) | MIDI → MusicXML, hand assignment, engraving | MIT |
| [DuET model weights](https://huggingface.co/Wayne42/DuET) (`Wayne42/DuET`) | the trained model | **CC BY-NC 4.0** |
| [Transkun](https://github.com/Yujia-Yan/Transkun) v2 | audio → performance MIDI | MIT (weights bundled in the package) |
| [MuseScore 4](https://musescore.org) | engraving to PDF / MSCZ / PNG | GPL v3 (not committed to this repository; the container image fetches the official AppImage from the MuseScore releases page at build time) |
| ffmpeg | audio decoding | LGPL/GPL depending on build |

`ncls` is pulled in as a transitive dependency of Transkun.

## Consequences for users

The DuET model weights are licensed **CC BY-NC 4.0 (non-commercial)**.
Accordingly:

* ScoreForge is offered free of charge for **personal, non-commercial use**.
* Transcripts produced by ScoreForge are **for personal, non-commercial use**.
* You may not use the output commercially without obtaining a separate
  commercial licence for the underlying weights.

Attribution: the notation model is DuET by Wei Zeng et al. (ICLR 2026) and
the audio frontend is Transkun by Yujia Yan et al. (ISMIR 2024).

## ScoreForge's own code

MIT — see `LICENSE`.
