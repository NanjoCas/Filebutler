"""Files written only for the held-out retrieval set (the `heldout` part of the merged library).

Both the dev set and the held-out set list this part under `library:`, so the library, its
term statistics and its distractors are the same whichever set is run. Add the files before
the dev run that picks the chunk size: a file added later changes IDF and the choice.
Frozen with the held-out set: changing a file changes the answers.

How to write the files (the content is yours; this module only provides the writers):
- New facts only: nothing asked in the dev set (eval/retrieval_set_20261024.yaml and
  retrieval_queries.yaml), and no wording copied from it.
- Every file a question needs gets one or two distractors on a nearby topic that share
  some of its words but not the answer.
- Late-page PDFs: put the fact on page 3 or later, and nowhere on pages 1–2 (the validator
  checks this).
- Non-PDF formats: Word (`docx`, needs macOS textutil), PowerPoint (`pptx`, slide text and
  speaker notes), Excel (`xlsx`, one or more sheets), web pages (`write` with HTML), Markdown
  and plain text (`write`).
- OCR: photos (`picture`, `turn=True` for a sideways shot) and scanned PDFs (`scanned_pdf`);
  short lines in a plain font read best.
- Paths are relative to the part, e.g. root / '会议/2026-11-组会纪要.docx'.

    def build(root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        docx(root / '会议/...docx', '...')
        pdf(root / '报告/...pdf', ['第 1 页', '第 2 页', '第 3 页：答案在这里'])
        picture(root / '照片/...jpg', ['第一行', '第二行'], turn=True)
        return root
"""
from pathlib import Path

from eval.ocr_cases import _picture as picture, _scanned_pdf as scanned_pdf  # noqa: F401  (writers for build)
from eval.retrieval_mixed import (  # noqa: F401  (writers for build)
    _docx as docx, _pdf as pdf, _pptx as pptx, _write as write, _xlsx as xlsx,
)


def build(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    # The held-out files go here (see the module docstring). Empty until they are written.
    return root
