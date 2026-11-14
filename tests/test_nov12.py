import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from zipfile import ZipFile

import support
from fastapi import HTTPException
from app.ai.service import TranslationService
from app.chat.connections import ConnectionManager
import test_cd_features as cd


class TranslationRechecks(unittest.TestCase):
    def setUp(self):
        cd.FeatureChecks.setUp(self)

    def test_membership_revoked_during_message_translation(self):
        translator = Mock()
        def translate(*args):
            self.rooms.leave("r", "owner")
            return "old result"
        translator.translate.side_effect = translate
        svc = TranslationService(self.db, self.rooms, translator)
        with self.assertRaises(HTTPException) as error:
            svc.translate_message("r", 1, "ko", "owner")
        self.assertEqual(error.exception.status_code, 403)

    def test_page_changed_during_translation_is_not_returned(self):
        media = Mock()
        media.asset.return_value = {"roomId":"r", "transcriptKey":"one"}
        media.live_asset.return_value = media.asset.return_value  # 삭제되지 않은 파일만 돌려주는 조회
        source = {"pages":["original"], "version":1}
        media.transcript.side_effect = lambda _: dict(source)
        translator = Mock()
        def translate(*args):
            source.update(pages=["corrected"], version=2)
            return "old result"
        translator.translate.side_effect = translate
        svc = TranslationService(self.db, self.rooms, translator, media=media)
        with self.assertRaises(HTTPException) as error:
            svc.translate_page("f", 1, "ko", "owner")
        self.assertEqual(error.exception.detail, "SOURCE_VERSION_CHANGED")

    def test_cached_message_also_rechecks_membership(self):
        svc = TranslationService(self.db, self.rooms, Mock())
        def cached(*args):
            self.rooms.leave("r", "owner")
            return "key", "cached result"
        with patch.object(svc, "cached", side_effect=cached), self.assertRaises(HTTPException):
            svc.translate_message("r", 1, "ko", "owner")


class ListAndPresenceChecks(unittest.IsolatedAsyncioTestCase):
    async def test_presence_failure_still_delivers_locally(self):
        manager = ConnectionManager(Mock(), Mock())
        manager.bus.presence = AsyncMock(side_effect=ConnectionError("Redis down"))
        manager.deliver = AsyncMock()
        await manager.publish_presence()
        manager.deliver.assert_awaited_once_with("", {"type":"presence:refresh"})

    def test_deletion_race_skips_only_missing_room(self):
        from app.rooms.routes import public_rooms
        repo = Mock()
        repo.all.return_value = [{"roomId":"a", "visibility":"public"}, {"roomId":"b", "visibility":"public"}]
        good = {"roomId":"a", "createdAt":1}
        with patch("app.rooms.routes.room_repo", repo), patch("app.rooms.routes.room_view", side_effect=[good, HTTPException(404, "deleted")]):
            self.assertEqual(public_rooms("owner"), [good])
        with patch("app.rooms.routes.room_repo", repo), patch("app.rooms.routes.room_view", side_effect=HTTPException(503, "database unavailable")):
            with self.assertRaises(HTTPException) as error:
                public_rooms("owner")
            self.assertEqual(error.exception.status_code, 503)

    def test_ocr_release_and_bootstrap_are_connected(self):
        from scripts.package_release import main
        main()
        root = Path(__file__).resolve().parents[1]
        with ZipFile(root / "dist/messenger.zip") as archive:
            self.assertIn("requirements-ocr.txt", archive.namelist())
            self.assertIn("scripts/prepare_ocr.py", archive.namelist())
        template = (root / "deploy/learner-lab.yaml").read_text()
        self.assertIn("pip install -r /opt/messenger/requirements-ocr.txt", template)
        self.assertIn("/opt/messenger/scripts/prepare_ocr.py", template)
