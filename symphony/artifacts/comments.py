from __future__ import annotations

import html
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from symphony.artifacts.file_manager import ArtifactFileManager
from symphony.domain.workflow import STAGES
from symphony.storage.db import utc_now


@dataclass(slots=True)
class TextMatch:
    raw_start: int
    raw_end: int
    occurrence_index: int


class ArtifactCommentManager:
    """Applies structured comment operations to artifact.html on the server."""

    def __init__(self, artifacts: ArtifactFileManager):
        self.artifacts = artifacts

    def add_comment(self, artifact_path: str, data: dict[str, Any]) -> dict[str, Any]:
        section = str(data.get("section") or "").strip()
        content = str(data.get("content") or "").strip()
        quote = str(data.get("quote") or "").strip()
        if not section:
            raise ValueError("comment section required")
        if not content:
            raise ValueError("comment content required")
        if section not in self._valid_sections():
            raise ValueError(f"invalid comment section: {section}")

        artifact_html = self.artifacts.read(artifact_path)
        state = self._read_state(artifact_html)
        comment_id = f"c_{uuid.uuid4().hex[:12]}"
        anchor_id = str(data.get("anchorId") or data.get("anchor_id") or "").strip()
        anchored = False

        if anchor_id and self._anchor_exists(state, anchor_id):
            artifact_html = self._append_comment_to_anchor(artifact_html, anchor_id, comment_id, state)
            anchored = True
        elif quote:
            anchor_id = f"a_{uuid.uuid4().hex[:12]}"
            artifact_html, anchored = self._insert_anchor(
                artifact_html,
                section=section,
                anchor_id=anchor_id,
                comment_id=comment_id,
                quote=quote,
                prefix=str(data.get("prefix") or ""),
                suffix=str(data.get("suffix") or ""),
                occurrence_index=self._optional_int(data.get("occurrenceIndex", data.get("occurrence_index"))),
            )
            if anchored:
                state["anchors"].append(
                    {
                        "id": anchor_id,
                        "section": section,
                        "quote": quote,
                        "commentIds": [comment_id],
                        "createdAt": utc_now(),
                    }
                )
            else:
                anchor_id = ""

        comment = {
            "id": comment_id,
            "anchorId": anchor_id or None,
            "section": section,
            "quote": quote,
            "content": content,
            "status": "pending",
            "anchored": anchored,
            "createdAt": utc_now(),
            "prefix": str(data.get("prefix") or ""),
            "suffix": str(data.get("suffix") or ""),
        }
        state["comments"].append(comment)
        artifact_html = self._write_state(artifact_html, state)
        self.artifacts.save(artifact_path, artifact_html)
        return {"comment": comment, "anchor": self._find_anchor(state, anchor_id) if anchor_id else None}

    def resolve_comment(self, artifact_path: str, comment_id: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        artifact_html = self.artifacts.read(artifact_path)
        state = self._read_state(artifact_html)
        comment = self._find_comment(state, comment_id)
        if not comment:
            raise KeyError(comment_id)
        status = str((data or {}).get("status") or "done")
        if status not in {"pending", "done"}:
            raise ValueError(f"invalid comment status: {status}")
        comment["status"] = status
        comment["updatedAt"] = utc_now()
        if status == "done":
            comment["resolvedAt"] = utc_now()
        anchor_id = str(comment.get("anchorId") or "")
        if anchor_id:
            artifact_html = self._refresh_anchor_status(artifact_html, state, anchor_id)
        artifact_html = self._write_state(artifact_html, state)
        self.artifacts.save(artifact_path, artifact_html)
        return {"comment": comment, "anchor": self._find_anchor(state, anchor_id) if anchor_id else None}

    def _insert_anchor(
        self,
        artifact_html: str,
        *,
        section: str,
        anchor_id: str,
        comment_id: str,
        quote: str,
        prefix: str,
        suffix: str,
        occurrence_index: int | None,
    ) -> tuple[str, bool]:
        section_match = self._section_match(artifact_html, section)
        if not section_match:
            return artifact_html, False
        section_open, section_body, section_close = section_match.group(1), section_match.group(3), section_match.group(4)
        match = self._find_text_match(section_body, quote, prefix, suffix, occurrence_index)
        if not match:
            return artifact_html, False
        opening = (
            f'<mark data-comment-anchor-id="{html.escape(anchor_id, quote=True)}" '
            f'data-comment-ids="{html.escape(comment_id, quote=True)}" data-comment-status="pending">'
        )
        updated_body = section_body[: match.raw_start] + opening + section_body[match.raw_start : match.raw_end] + "</mark>" + section_body[match.raw_end :]
        updated_section = section_open + updated_body + section_close
        return artifact_html[: section_match.start()] + updated_section + artifact_html[section_match.end() :], True

    def _append_comment_to_anchor(self, artifact_html: str, anchor_id: str, comment_id: str, state: dict[str, Any]) -> str:
        anchor = self._find_anchor(state, anchor_id)
        if anchor is not None:
            ids = anchor.setdefault("commentIds", [])
            if comment_id not in ids:
                ids.append(comment_id)

        pattern = re.compile(
            rf"<mark\b(?=[^>]*\bdata-comment-anchor-id=(['\"]){re.escape(anchor_id)}\1)([^>]*)>",
            flags=re.I,
        )

        def replace(match: re.Match[str]) -> str:
            tag = match.group(0)
            ids_match = re.search(r"\bdata-comment-ids=(['\"])(.*?)\1", tag, flags=re.I)
            ids = ids_match.group(2).split() if ids_match else []
            if comment_id not in ids:
                ids.append(comment_id)
            tag = self._set_attr(tag, "data-comment-ids", " ".join(ids))
            return self._set_attr(tag, "data-comment-status", "pending")

        return pattern.sub(replace, artifact_html, count=1)

    def _refresh_anchor_status(self, artifact_html: str, state: dict[str, Any], anchor_id: str) -> str:
        anchor = self._find_anchor(state, anchor_id)
        if not anchor:
            return artifact_html
        ids = set(anchor.get("commentIds") or [])
        pending = any(
            comment.get("id") in ids and str(comment.get("status") or "pending").lower() != "done"
            for comment in state.get("comments", [])
            if isinstance(comment, dict)
        )
        status = "pending" if pending else "done"
        pattern = re.compile(
            rf"<mark\b(?=[^>]*\bdata-comment-anchor-id=(['\"]){re.escape(anchor_id)}\1)([^>]*)>",
            flags=re.I,
        )
        return pattern.sub(lambda match: self._set_attr(match.group(0), "data-comment-status", status), artifact_html, count=1)

    @staticmethod
    def _set_attr(tag: str, name: str, value: str) -> str:
        escaped = html.escape(value, quote=True)
        if re.search(rf"\b{re.escape(name)}=(['\"]).*?\1", tag, flags=re.I):
            return re.sub(rf"\b{re.escape(name)}=(['\"]).*?\1", f'{name}="{escaped}"', tag, count=1, flags=re.I)
        return tag[:-1] + f' {name}="{escaped}">'

    def _section_match(self, artifact_html: str, section: str) -> re.Match[str] | None:
        escaped = re.escape(section)
        return re.search(
            rf"(<section\b(?=[^>]*\bid=(['\"]){escaped}\2)[^>]*>)(.*?)(</section>)",
            artifact_html,
            flags=re.I | re.S,
        )

    def _find_text_match(self, section_body: str, quote: str, prefix: str, suffix: str, occurrence_index: int | None) -> TextMatch | None:
        visible, mapping = self._visible_text_map(section_body)
        if not quote or not visible:
            return None
        candidates: list[tuple[int, int]] = []
        start = 0
        while True:
            index = visible.find(quote, start)
            if index < 0:
                break
            candidates.append((index, index + len(quote)))
            start = index + max(len(quote), 1)
        if not candidates:
            return None
        if occurrence_index is not None and 0 <= occurrence_index < len(candidates):
            chosen_index = occurrence_index
            chosen = candidates[chosen_index]
        else:
            scored = [(self._anchor_score(visible, start, end, prefix, suffix), i, (start, end)) for i, (start, end) in enumerate(candidates)]
            _, chosen_index, chosen = max(scored, key=lambda item: (item[0], -item[1]))
        raw_start = mapping[chosen[0]][0]
        raw_end = mapping[chosen[1] - 1][1]
        return TextMatch(raw_start=raw_start, raw_end=raw_end, occurrence_index=chosen_index)

    @staticmethod
    def _anchor_score(visible: str, start: int, end: int, prefix: str, suffix: str) -> int:
        score = 0
        if prefix and visible[:start].endswith(prefix):
            score += len(prefix)
        if suffix and visible[end:].startswith(suffix):
            score += len(suffix)
        return score

    @staticmethod
    def _visible_text_map(raw: str) -> tuple[str, list[tuple[int, int]]]:
        chars: list[str] = []
        mapping: list[tuple[int, int]] = []
        i = 0
        while i < len(raw):
            if raw[i] == "<":
                close = raw.find(">", i + 1)
                if close < 0:
                    break
                i = close + 1
                continue
            if raw[i] == "&":
                close = raw.find(";", i + 1, i + 12)
                if close > 0:
                    decoded = html.unescape(raw[i : close + 1])
                    for char in decoded:
                        chars.append(char)
                        mapping.append((i, close + 1))
                    i = close + 1
                    continue
            chars.append(raw[i])
            mapping.append((i, i + 1))
            i += 1
        return "".join(chars), mapping

    def _read_state(self, artifact_html: str) -> dict[str, Any]:
        match = self._comments_script_match(artifact_html)
        if not match:
            return {"comments": [], "anchors": []}
        try:
            payload = json.loads(match.group(2).strip() or '{"comments":[]}')
        except json.JSONDecodeError:
            payload = {"comments": [], "anchors": []}
        if not isinstance(payload, dict):
            payload = {"comments": [], "anchors": []}
        comments = payload.get("comments") if isinstance(payload.get("comments"), list) else []
        anchors = payload.get("anchors") if isinstance(payload.get("anchors"), list) else []
        payload["comments"] = [comment for comment in comments if isinstance(comment, dict)]
        payload["anchors"] = [anchor for anchor in anchors if isinstance(anchor, dict)]
        return payload

    def _write_state(self, artifact_html: str, state: dict[str, Any]) -> str:
        match = self._comments_script_match(artifact_html)
        if not match:
            raise ValueError("artifact comments script missing")
        payload = json.dumps(state, ensure_ascii=False, indent=2)
        return artifact_html[: match.start(2)] + "\n    " + payload + "\n  " + artifact_html[match.end(2) :]

    @staticmethod
    def _comments_script_match(artifact_html: str) -> re.Match[str] | None:
        return re.search(r'(<script[^>]*id=["\']harness-comments["\'][^>]*>)(.*?)(</script>)', artifact_html, flags=re.S | re.I)

    @staticmethod
    def _find_comment(state: dict[str, Any], comment_id: str) -> dict[str, Any] | None:
        return next((comment for comment in state.get("comments", []) if isinstance(comment, dict) and comment.get("id") == comment_id), None)

    @staticmethod
    def _find_anchor(state: dict[str, Any], anchor_id: str) -> dict[str, Any] | None:
        return next((anchor for anchor in state.get("anchors", []) if isinstance(anchor, dict) and anchor.get("id") == anchor_id), None)

    def _anchor_exists(self, state: dict[str, Any], anchor_id: str) -> bool:
        return self._find_anchor(state, anchor_id) is not None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _valid_sections() -> set[str]:
        sections = {section for spec in STAGES.values() for section in spec["editable_sections"]}
        return sections | {"background", "interaction", "solution", "task-breakdown", "validation", "history", "learning"}
