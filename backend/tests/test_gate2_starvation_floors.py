"""Gate 2 — tier-budget starvation floors (tier_ladder ledger).

The measured failure (Gate 1 audit §5/§9.3): the production-global
extra accounts (40 searches / 20 downloads, D-B2) were spent in
scene order through a reserve-plus-fixed-lookahead gate, and the
lookahead under-counted the needs in scenes the stage had not
reached — Petito recorded 21/96 tier attempts budget_exhausted
with sentence 23 executing ZERO queries; Container 52/88. The
speculative (metadata-absence bypass) route shared the download
account with NO reserve gate at all, so early beats' speculative
downloads could drain the account later sentences' escalation
needed.

The fix is a floor / beyond-floor split inside the SAME caps:
half of each account is a floor reserve only first attempts may
draw on; beyond-floor consumers (search deepening, download
deepening, speculative downloads — the latter two sharing one
sub-account) spend only from the other half, and speculative
spends must additionally leave one slot per registered need still
waiting for its first download.

Invariant proven here: processing needs in scene order, a later
need's first tier attempt is never left with zero executed
queries (or zero first-download availability) while budget
remains unspent or was spent on earlier scenes' beyond-floor
consumption. Starvation is possible only at genuine exhaustion,
where at least half of everything spent went to first attempts.
"""

from services import tier_ladder

CAP_S = tier_ladder.PRODUCTION_MAX_EXTRA_SEARCHES
CAP_D = tier_ladder.PRODUCTION_MAX_EXTRA_DOWNLOADS


def _walk_need(ledger, key, batches=(2, 3, 2)):
    """One need's full ladder walk against the ledger, in the
    pipeline's shape: per-tier batches allocated then spent.
    Returns the number of searches actually executed."""
    executed = 0
    for batch in batches:
        allowed = ledger.allocate_searches(batch, need_key=key)
        if allowed > 0:
            ledger.spend_searches(allowed, need_key=key)
            executed += allowed
    return executed


# ---------------------------------------------------------------------------
# Searches: deepening cannot consume the floor reserve
# ---------------------------------------------------------------------------

def test_deepening_searches_stop_at_the_floor_split():
    ledger = tier_ladder.EscalationLedger()
    # One hungry need walks batch after batch: its first search is
    # breadth, everything after is deepening, and deepening stops
    # at the sub-account even though the production account still
    # holds the whole floor reserve.
    executed = _walk_need(ledger, "hog", batches=(3,) * 40)
    assert ledger.deepening_searches == tier_ladder.DEEPENING_SEARCH_BUDGET
    assert executed == 1 + tier_ladder.DEEPENING_SEARCH_BUDGET
    assert ledger.searches == executed
    assert ledger.searches < CAP_S
    # A later need's first attempt still executes — from the floor.
    assert ledger.allocate_searches(3, need_key="late") == 1
    ledger.spend_searches(1, need_key="late")
    summary = ledger.summary()
    assert summary["floor_search_reserve"] == tier_ladder.FLOOR_SEARCH_RESERVE
    assert summary["deepening_search_budget"] == (
        tier_ladder.DEEPENING_SEARCH_BUDGET
    )


def test_search_starvation_invariant_in_scene_order():
    """Thirty needs arrive in scene order, each demanding a full
    three-tier walk (7 searches). The first twenty — the floor
    reserve's worth — each execute at least one real query; any
    need after that can starve only at genuine exhaustion, with
    deepening never exceeding breadth."""
    ledger = tier_ladder.EscalationLedger()
    executed = [
        _walk_need(ledger, f"need_{n}") for n in range(30)
    ]
    assert all(count >= 1 for count in executed[:CAP_S // 2])
    assert ledger.searches == CAP_S
    assert ledger.breadth_searches >= CAP_S // 2
    assert ledger.deepening_searches <= ledger.breadth_searches
    # Every starved need arrived only after the account was spent
    # at least half on first attempts.
    starved = [n for n, count in enumerate(executed) if count == 0]
    assert all(n >= CAP_S // 2 for n in starved)


def test_unattributed_spends_keep_legacy_semantics():
    """The split binds attributed escalation only: the historical
    no-need-key account behaviour is unchanged."""
    ledger = tier_ladder.EscalationLedger()
    assert ledger.allocate_searches(CAP_S) == CAP_S
    ledger.spend_searches(CAP_S)
    assert ledger.allocate_searches(1) == 0
    assert ledger.allocate_searches(1, need_key="late") == 0


# ---------------------------------------------------------------------------
# Downloads: speculative + deepening share one beyond-floor account
# ---------------------------------------------------------------------------

def test_speculative_downloads_cannot_drain_escalation_floor():
    ledger = tier_ladder.EscalationLedger()
    for key in ("n1", "n2", "n3"):
        ledger.register_need(key)
    spent = 0
    beat = 0
    while ledger.can_spend_speculative_download(
        beat_key=("scene", beat)
    ):
        ledger.spend_speculative_download(beat_key=("scene", beat))
        spent += 1
        beat += 1  # distinct beats: the per-beat cap is not what binds
    # Speculative spending stopped at the beyond-floor sub-account,
    # with the floor half of the production account untouched.
    assert spent == tier_ladder.BEYOND_FLOOR_DOWNLOAD_BUDGET
    assert ledger.downloads + ledger.speculative_downloads < CAP_D
    # Every registered need's first escalation download is intact.
    for key in ("n1", "n2", "n3"):
        assert ledger.can_spend_download(need_key=key)
        assert ledger.can_draw_verified_download(need_key=key)
        ledger.spend_download(need_key=key)
    summary = ledger.summary()
    assert summary["beyond_floor_download_spends"] == spent
    assert summary["floor_download_reserve"] == (
        tier_ladder.FLOOR_DOWNLOAD_RESERVE
    )


def test_speculative_downloads_leave_registered_first_downloads():
    """With twelve registered needs waiting for a first download,
    speculative spends stop early enough that all twelve first
    downloads still fit in the account."""
    ledger = tier_ladder.EscalationLedger()
    keys = [f"need_{n}" for n in range(12)]
    for key in keys:
        ledger.register_need(key)
    beat = 0
    while ledger.can_spend_speculative_download(beat_key=("s", beat)):
        ledger.spend_speculative_download(beat_key=("s", beat))
        beat += 1
    # Speculative spending stopped with all twelve first-download
    # slots still standing: the gate prices the prospective spend,
    # so the last allowed spend brings speculative to exactly
    # 20 - 12 = 8 and not one slot further.
    assert ledger.speculative_downloads == 8
    for key in keys:
        assert ledger.can_spend_download(need_key=key)
        ledger.spend_download(need_key=key)
    assert (
        ledger.downloads + ledger.speculative_downloads
        == 12 + 8
    )


def test_escalation_deepening_shares_the_beyond_floor_account():
    """Speculative spends that fill the beyond-floor sub-account
    also close escalation deepening — the two routes share ONE
    sub-account — while a first (floor) download stays open."""
    ledger = tier_ladder.EscalationLedger()
    beat = 0
    while ledger.can_spend_speculative_download(beat_key=("s", beat)):
        ledger.spend_speculative_download(beat_key=("s", beat))
        beat += 1
    assert ledger.speculative_downloads == (
        tier_ladder.BEYOND_FLOOR_DOWNLOAD_BUDGET
    )
    ledger.register_need("a")
    assert ledger.can_spend_download(need_key="a")  # breadth/floor
    ledger.spend_download(need_key="a")
    assert not ledger.can_spend_download(need_key="a")  # deepening
    assert not ledger.can_draw_verified_download(need_key="a")
    assert ledger.deepening_downloads == 0


def test_download_starvation_invariant_in_scene_order():
    """Twenty needs in scene order, each first taking its floor
    download and then trying to deepen five times: the first ten
    needs all secure their first download, and total deepening
    never exceeds the beyond-floor sub-account."""
    ledger = tier_ladder.EscalationLedger()
    first_downloads = []
    for n in range(20):
        key = f"need_{n}"
        ledger.register_need(key)
        got_first = False
        if ledger.can_spend_download(need_key=key):
            ledger.spend_download(need_key=key)
            got_first = True
            for _ in range(5):
                if ledger.can_spend_download(need_key=key):
                    ledger.spend_download(need_key=key)
        first_downloads.append(got_first)
    assert all(first_downloads[: CAP_D // 2])
    assert ledger.deepening_downloads <= (
        tier_ladder.BEYOND_FLOOR_DOWNLOAD_BUDGET
    )
    assert ledger.downloads + ledger.speculative_downloads <= CAP_D


def test_caps_unchanged_by_the_split():
    """D-B2 standing rule: the split reallocates INSIDE the caps;
    it does not raise them, and the per-tier budgets are untouched."""
    assert tier_ladder.PRODUCTION_MAX_EXTRA_SEARCHES == 40
    assert tier_ladder.PRODUCTION_MAX_EXTRA_DOWNLOADS == 20
    assert tier_ladder.TIER_QUERY_BUDGETS == {
        "related": 2, "activity_broll": 3, "atmospheric": 2,
    }
    assert tier_ladder.TIER_DOWNLOAD_ATTEMPTS == 2
    assert (
        tier_ladder.FLOOR_SEARCH_RESERVE
        + tier_ladder.DEEPENING_SEARCH_BUDGET
        == CAP_S
    )
    assert (
        tier_ladder.FLOOR_DOWNLOAD_RESERVE
        + tier_ladder.BEYOND_FLOOR_DOWNLOAD_BUDGET
        == CAP_D
    )
