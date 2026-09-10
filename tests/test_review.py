import pytest

from orchestrator import review
from orchestrator.models import VideoRequest, VideoResult


def make_pending(output_dir, topic_id="t1", data=b"fake-mp4"):
    pending = review.stage_dir(output_dir, review.PENDING)
    mp4 = pending / f"{topic_id}.mp4"
    mp4.write_bytes(data)
    result = VideoResult(request=VideoRequest(topic_id=topic_id, subject="S"), task_id="task",
                         video_path=str(mp4))
    return review.stage_pending(result, output_dir)


def test_stage_pending_writes_sidecar_with_hash(tmp_path):
    sidecar = make_pending(tmp_path)
    r = review.read_sidecar(sidecar)
    assert r.video_path == "t1.mp4"
    assert r.sha256 == review.sha256_file(tmp_path / "pending" / "t1.mp4")


def test_stage_pending_rejects_video_outside_pending(tmp_path):
    mp4 = tmp_path / "elsewhere.mp4"
    mp4.write_bytes(b"x")
    r = VideoResult(request=VideoRequest(topic_id="x", subject="S"), task_id="t", video_path=str(mp4))
    with pytest.raises(ValueError):
        review.stage_pending(r, tmp_path)


def test_nothing_is_uploadable_until_approved(tmp_path):
    make_pending(tmp_path)
    assert review.load_approved(tmp_path) == []
    review.approve("t1", tmp_path)
    items = review.load_approved(tmp_path)
    assert [r.request.topic_id for _, r in items] == ["t1"]
    assert not (tmp_path / "pending" / "t1.mp4").exists()


def test_manual_move_also_counts_as_approval(tmp_path):
    sidecar = make_pending(tmp_path)
    approved = review.stage_dir(tmp_path, review.APPROVED)
    (tmp_path / "pending" / "t1.mp4").rename(approved / "t1.mp4")
    sidecar.rename(approved / "t1.json")
    assert len(review.load_approved(tmp_path)) == 1


def test_modified_video_after_approval_is_skipped(tmp_path):
    make_pending(tmp_path)
    review.approve("t1", tmp_path)
    (tmp_path / "approved" / "t1.mp4").write_bytes(b"tampered")
    assert review.load_approved(tmp_path) == []


def test_reject_and_dry_run(tmp_path):
    make_pending(tmp_path, "a")
    make_pending(tmp_path, "b")
    review.approve("a", tmp_path, dry_run=True)
    assert (tmp_path / "pending" / "a.json").exists()
    review.reject("b", tmp_path)
    assert (tmp_path / "rejected" / "b.json").exists()
    with pytest.raises(FileNotFoundError):
        review.approve("missing", tmp_path)


def test_mark_uploaded_records_id_and_moves(tmp_path):
    make_pending(tmp_path)
    review.approve("t1", tmp_path)
    sidecar, result = review.load_approved(tmp_path)[0]
    dest = review.mark_uploaded(sidecar, result, "vid123", tmp_path)
    assert review.read_sidecar(dest).youtube_video_id == "vid123"
    assert (tmp_path / "uploaded" / "t1.mp4").exists()


def test_cli_list_and_approve_dry_run(settings, capsys):
    make_pending(settings.output_dir)
    assert review.main(["list"]) == 0
    assert "t1" in capsys.readouterr().out
    assert review.main(["--dry-run", "approve", "t1"]) == 0
    assert (settings.output_dir / "pending" / "t1.json").exists()
