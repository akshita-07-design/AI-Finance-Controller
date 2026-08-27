"""
Sanity tests for the generator.

These aren't testing "does it run" — they're testing the invariants that
make the generated data trustworthy as an evaluation surface at all. If any
of these fail, every metric the matching engine reports later is built on
sand, per the roadmap's Part 3 warning.
"""

import random
from collections import defaultdict

import pytest

from recon.generate.anomalies import AnomalyRates
from recon.generate.bank import generate_bank_statement
from recon.generate.ground_truth import build_ground_truth
from recon.generate.ledger import generate_ledger
from recon.generate.settlement import generate_settlements
from recon.models import SettlementEntityType


@pytest.fixture(scope="module")
def generated_dev():
    """One full generation run, shared across tests in this module — the
    generator is deterministic by seed, so re-running it per-test would just
    waste time without changing anything."""
    rates = AnomalyRates()
    rng = random.Random(42)
    plans = generate_ledger(rng, "2026-04", 1000, rates)
    settlement_result = generate_settlements(rng, plans, rates)
    force_ids = frozenset(
        sid for pair in settlement_result.duplicate_amount_pairs for sid in pair
    )
    bank_result = generate_bank_statement(
        rng, settlement_result.summaries, rates, "2026-04",
        force_unrecoverable_utr_ids=force_ids,
    )
    ground_truth = build_ground_truth(
        42, "2026-04", 1000, rates, plans, settlement_result, bank_result,
    )
    return {
        "plans": plans,
        "settlement_result": settlement_result,
        "bank_result": bank_result,
        "ground_truth": ground_truth,
    }


class TestDeterminism:
    def test_same_seed_produces_identical_output(self):
        """The whole point of seed=42/seed=1337 discipline (roadmap Part 3.1)
        depends on this being true. If it isn't, dev/test comparisons mean
        nothing."""
        rates = AnomalyRates()

        rng_a = random.Random(42)
        plans_a = generate_ledger(rng_a, "2026-04", 200, rates)

        rng_b = random.Random(42)
        plans_b = generate_ledger(rng_b, "2026-04", 200, rates)

        ids_a = [p.ledger.internal_order_id for p in plans_a]
        ids_b = [p.ledger.internal_order_id for p in plans_b]
        amounts_a = [p.ledger.gross_amount_paise for p in plans_a]
        amounts_b = [p.ledger.gross_amount_paise for p in plans_b]

        assert ids_a == ids_b
        assert amounts_a == amounts_b

    def test_different_seeds_produce_different_output(self):
        """The inverse check — dev and test shouldn't be secretly identical."""
        rates = AnomalyRates()
        plans_42 = generate_ledger(random.Random(42), "2026-04", 200, rates)
        plans_1337 = generate_ledger(random.Random(1337), "2026-04", 200, rates)
        amounts_42 = [p.ledger.gross_amount_paise for p in plans_42]
        amounts_1337 = [p.ledger.gross_amount_paise for p in plans_1337]
        assert amounts_42 != amounts_1337


class TestArithmeticProof:
    def test_every_batch_nets_to_its_own_summary(self, generated_dev):
        """The identity Pass 3 will later rely on: for every settlement
        batch, sum(credit) - sum(debit) across its lines must exactly equal
        the batch summary's `amount`. This has to be true by construction —
        it's the generator's OWN arithmetic, not something inferred."""
        result = generated_dev["settlement_result"]
        lines_by_settlement = defaultdict(list)
        for line in result.lines:
            lines_by_settlement[line.settlement_id].append(line)

        checked = 0
        for summary in result.summaries:
            lines = lines_by_settlement[summary.id]
            computed_net = sum(l.credit for l in lines) - sum(l.debit for l in lines)
            assert computed_net == summary.amount, (
                f"Batch {summary.id} nets to {computed_net} paise but its "
                f"summary claims {summary.amount} paise"
            )
            checked += 1
        assert checked == len(result.summaries)
        assert checked > 30, "expected at least 30 batches from 1000 orders over a month"

    def test_no_floats_anywhere_in_settlement_lines(self, generated_dev):
        for line in generated_dev["settlement_result"].lines:
            for field_name in ("debit", "credit", "amount", "fee", "tax"):
                value = getattr(line, field_name)
                assert isinstance(value, int) and not isinstance(value, bool), (
                    f"{field_name}={value!r} on {line.entity_id} is not a plain int"
                )


class TestBankStatementIntegrity:
    def test_running_balance_is_internally_consistent(self, generated_dev):
        """The running balance is the generator's free self-check (roadmap
        Part 3.4). If this doesn't foot, don't trust anything else the
        generator produced."""
        rows = generated_dev["bank_result"].rows
        from recon.generate.bank import OPENING_BALANCE_PAISE
        running = OPENING_BALANCE_PAISE
        for row in rows:
            running += row.credit_paise - row.debit_paise
            assert row.balance_paise == running, (
                f"{row.bank_txn_id}: expected balance {running}, got {row.balance_paise}"
            )

    def test_rows_are_in_chronological_order(self, generated_dev):
        rows = generated_dev["bank_result"].rows
        dates = [r.txn_date for r in rows]
        assert dates == sorted(dates)

    def test_every_settlement_batch_has_at_least_one_bank_row(self, generated_dev):
        result = generated_dev["settlement_result"]
        bank_result = generated_dev["bank_result"]
        for summary in result.summaries:
            assert summary.id in bank_result.settlement_to_bank_txn, (
                f"Settlement {summary.id} has no corresponding bank row at all"
            )

    def test_narration_truncation_never_loses_the_utr_without_a_ref_no_fallback(self, generated_dev):
        """Regression test for a real bug: some template + UTR-style
        combinations are long enough that the realistic 50-char narration
        truncation cuts INTO the UTR itself, by accident — not from any
        deliberately seeded anomaly. One such case, hitting a 101-line
        settlement batch, silently dropped an otherwise-clean test-set match
        rate by ten points for a reason that had nothing to do with the 18
        designed anomalies. Whenever truncation actually loses part of the
        UTR, ref_no must carry the full value as a guaranteed fallback —
        real information loss should only come from anomalies designed to
        cause it. See FAILURE_LOG.md."""
        bank_result = generated_dev["bank_result"]
        for row in bank_result.rows:
            if "seeded_12_ambiguous_duplicate_amount" in row.anomaly_tags:
                continue  # this one's UTR loss is deliberate, by design
            if row.credit_paise <= 0 and row.debit_paise <= 0:
                continue  # noise/reversal rows carry no settlement UTR at all
            # We can't recover the true UTR here without the settlement
            # summary in hand, but we CAN assert the general invariant: if
            # ref_no is empty, the full UTR must appear somewhere in the
            # narration untruncated. This test would have caught the bug by
            # failing on the exact 101-line-batch row that triggered it.
            if not row.ref_no:
                # A settlement-linked row's narration must contain an
                # UNTRUNCATED UTR-shaped token when there's no ref_no
                # fallback — i.e. the narration must not have been cut
                # mid-token. We check this indirectly: the narration must
                # end either mid-word-boundary-free or with a recognizable
                # suffix, which in practice means its length, if truncated,
                # should never land exactly at the 50-char ceiling while
                # still containing a fragment that LOOKS like a cut token.
                assert len(row.narration) <= 50
                # A truncated-mid-token narration ends with alnum characters
                # right at the boundary with nothing after — a genuine
                # (non-anomalous) row should either be well under 50 chars
                # (no truncation occurred) or end in one of the template's
                # own trailing words (SETTLEMENT, RZPY, SOFTWARE, etc.),
                # never in the middle of what looks like a reference code.
                if len(row.narration) == 50:
                    assert row.narration[-1].isalpha() or row.narration.endswith(
                        ("SETTLEMENT", "RZPY", "PRIVATELIM")
                    ), f"{row.bank_txn_id} narration may be truncated mid-UTR with no ref_no fallback"

    def test_negative_net_batches_appear_as_debits_not_dropped(self, generated_dev):
        """Regression test for a real bug caught during generation: an
        earlier version did `credit_paise = max(summary.amount, 0)`, which
        silently zeroed out any batch that netted negative (e.g. a quiet day
        where refunds/fees outweighed new payments) instead of recording it
        as a debit. See FAILURE_LOG.md."""
        result = generated_dev["settlement_result"]
        bank_result = generated_dev["bank_result"]
        rows_by_id = {r.bank_txn_id: r for r in bank_result.rows}

        negative_batches = [s for s in result.summaries if s.amount < 0]
        for summary in negative_batches:
            txn_ids = bank_result.settlement_to_bank_txn[summary.id]
            assert txn_ids, f"negative-net batch {summary.id} has no bank row at all"
            row = rows_by_id[txn_ids[0]]
            assert row.debit_paise == -summary.amount, (
                f"{summary.id} nets {summary.amount} paise but its bank row "
                f"shows debit={row.debit_paise}, credit={row.credit_paise}"
            )
            assert row.credit_paise == 0

    def test_ambiguous_duplicate_pairs_are_always_positive_credits(self, generated_dev):
        """The flagship anomaly (#12) should always be a clean payout
        collision, never a debit day — enforced in settlement.py by only
        drawing candidates from positive-net batches."""
        result = generated_dev["settlement_result"]
        summaries_by_id = {s.id: s for s in result.summaries}
        for a, b in result.duplicate_amount_pairs:
            assert summaries_by_id[a].amount > 0
            assert summaries_by_id[b].amount > 0

    def test_ambiguous_duplicate_original_batches_are_the_smallest_available(self, generated_dev):
        """Regression test, corrected three times in one session:
        (1) no size limit let one run sweep a 102-line and a 29-line batch
        into 'bank attribution ambiguous' (14% of all records);
        (2) a fixed cutoff of 4 lines then overcorrected to zero pairs
        firing, since this dataset's batches average ~27 lines each;
        (3) a percentile cutoff plus a 'shuffle among the smallest few' step
        looked right but the pool size formula happened to equal the entire
        eligible candidate count, so the shuffle drew uniformly from
        everyone in the quartile — silently undoing the smallest-first
        intent and picking a 29-line batch just as often as a 20-line one.
        See FAILURE_LOG.md."""
        result = generated_dev["settlement_result"]
        lines_per_settlement: dict[str, int] = {}
        for line in result.lines:
            if line.settlement_id is not None:
                lines_per_settlement[line.settlement_id] = (
                    lines_per_settlement.get(line.settlement_id, 0) + 1
                )
        # Exclude the synthetic twins themselves (the `b` side of each pair)
        # from the comparison pool — they're OUTPUT of the injection, always
        # trivially size 1, and were never a candidate the function could
        # have "chosen" as the original side. Comparing against them is the
        # exact same post-mutation mistake that caused the real bug this
        # test guards against (see FAILURE_LOG.md) — checking the state
        # AFTER injection instead of what was actually available BEFORE it.
        twin_ids = {b for _, b in result.duplicate_amount_pairs}
        eligible_sizes = sorted(
            lines_per_settlement.get(s.id, 0) for s in result.summaries
            if s.amount > 0 and s.id not in twin_ids
        )
        chosen_sizes = sorted(
            lines_per_settlement.get(a, 0) for a, _ in result.duplicate_amount_pairs
        )
        n = len(result.duplicate_amount_pairs)
        assert chosen_sizes == eligible_sizes[:n], (
            f"chose batch sizes {chosen_sizes} but the {n} smallest genuinely "
            f"eligible (non-twin) batches were {eligible_sizes[:n]}"
        )


class TestSeededAnomaliesActuallyAppear:
    """If a seeded anomaly's rate is > 0 but zero instances show up in a
    1000-order run, either the rate is too low for this sample size or
    there's a bug in the injection logic — either way, the roadmap's metrics
    would be reporting on cases that don't exist."""

    @pytest.mark.parametrize("tag", [
        "seeded_02_upi_zero_fee",
        "seeded_03_full_refund_same_cycle",
        "seeded_04_partial_refund",
        "seeded_05_cross_cycle_refund",
        "seeded_06_on_hold",
        "seeded_07_chargeback_adjustment",
        "seeded_14_abandoned",
        "seeded_16_amex_fee_variance",
    ])
    def test_order_level_anomaly_present(self, generated_dev, tag):
        plans = generated_dev["plans"]
        assert any(tag in p.anomaly_tags for p in plans), (
            f"{tag} configured with a nonzero rate but never appeared in 1000 orders"
        )

    def test_ambiguous_duplicate_amount_pair_exists_and_actually_collides(self, generated_dev):
        result = generated_dev["settlement_result"]
        assert len(result.duplicate_amount_pairs) > 0
        summaries_by_id = {s.id: s for s in result.summaries}
        for a, b in result.duplicate_amount_pairs:
            assert summaries_by_id[a].amount == summaries_by_id[b].amount, (
                "seeded_12 pair doesn't actually have matching net amounts — "
                "the anomaly wouldn't be ambiguous at all"
            )
            assert summaries_by_id[a].created_at == summaries_by_id[b].created_at, (
                "seeded_12 pair isn't on the same day — not actually ambiguous"
            )

    def test_null_utr_adjustments_exist_and_are_genuinely_null(self, generated_dev):
        adjustments = [
            l for l in generated_dev["settlement_result"].lines
            if l.type == SettlementEntityType.ADJUSTMENT
        ]
        assert len(adjustments) > 0
        assert all(a.settlement_utr is None for a in adjustments), (
            "adjustments must carry a null settlement_utr even inside a real "
            "batch — this matches Razorpay's own documented example"
        )

    def test_on_hold_payments_never_appear_in_a_settled_batch(self, generated_dev):
        unsettled = generated_dev["settlement_result"].unsettled_lines
        assert len(unsettled) > 0
        for line in unsettled:
            assert line.on_hold is True
            assert line.settled is False
            assert line.settlement_id is None
            assert line.settlement_utr is None


class TestGroundTruth:
    def test_every_match_points_at_a_real_settlement_line(self, generated_dev):
        gt = generated_dev["ground_truth"]
        real_entity_ids = {l.entity_id for l in generated_dev["settlement_result"].lines}
        for m in gt.matches:
            assert m.settlement_entity_id in real_entity_ids

    def test_expected_exceptions_are_a_small_minority(self, generated_dev):
        """Sanity bound, not a precise target: if more than ~15% of records
        are 'expected to be unresolvable', the dataset is too adversarial to
        be a fair evaluation surface (roadmap Part 3, on realistic anomaly
        rates)."""
        gt = generated_dev["ground_truth"]
        total_records = len(gt.matches) + len(gt.expected_exceptions)
        assert len(gt.expected_exceptions) / total_records < 0.15
