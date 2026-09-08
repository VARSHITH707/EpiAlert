"""EpiAlert Two-of-Two Confirmation Rule

Implements the three-state confirmation rule that converts raw CUSUM and EWMA
statistics into a contamination-resistant alert decision.

The rule requires BOTH statistics to exceed their limits before confirming an
alert. This is the direct analogue of Yi et al.'s Amber grade (>=2 of 4
concordant models, Youden=0.651; sens 0.739, spec 0.912). With two statistics,
requiring both is the intersection of two error rates rather than their union.

The cost is possible delay when one statistic responds first; quantify that
delay in Phase C3, do not assume it.

States:
  IN_CONTROL  — neither CUSUM nor EWMA exceeds its limit
  PROVISIONAL — exactly one exceeds (log + queue, NO community alert, a_t = 0)
  CONFIRMED   — both exceed (a_t = 1, baseline-ineligible, localisation + alert)

The feedback loop: when a_t = 1, the confirmed week is excluded from the
adaptive baseline (B_t) on subsequent weeks. This is what makes the baseline
contamination-resistant. See src/detection.baseline.compute_adaptive_baseline.
"""

from dataclasses import dataclass, field
from typing import Optional

# Locked parameters — see BUILD directive section 4
# N_C: >=2 distinct signalling streets in one cluster, same disease, same window
N_C = 2
# N_V: >=2 clusters meeting the cluster condition
N_V = 2


@dataclass
class ConfirmationState:
    """Tracks confirmation state across weeks for a single (disease, spatial_unit).

    This is the per-stream state that carries the PROVISIONAL queue and
    confirmed-count history. For spatial escalation (Phase B3), each
    (disease, street/cluster/village) gets its own ConfirmationState.
    """

    last_status: str = "IN_CONTROL"
    provisional_queue_count: int = 0
    confirmed_count: int = 0
    prev_cusum_S: float = 0.0
    prev_ewma_Z: float = 0.0
    # History of a_t values for feedback to baseline
    a_t_history: dict = field(default_factory=dict)

    def record_confirmed(self, week_num: int) -> None:
        """Record a confirmed week — writes a_t=1 into history."""
        self.a_t_history[week_num] = 1
        self.confirmed_count += 1
        self.last_status = "CONFIRMED"

    def record_provisional(self, week_num: int) -> None:
        """Record a provisional week — queues for investigation, a_t stays 0."""
        self.provisional_queue_count += 1
        self.last_status = "PROVISIONAL"
        # a_t stays 0; week stays in baseline

    def record_in_control(self, week_num: int) -> None:
        """Record an in-control week."""
        self.a_t_history[week_num] = 0
        self.last_status = "IN_CONTROL"

    def get_a_t(self, week_num: int) -> int:
        """Return a_t for a given week (0 or 1)."""
        return self.a_t_history.get(week_num, 0)


def confirm(
    week_num: int,
    cusum_result: dict,
    ewma_result: dict,
    state: Optional[ConfirmationState] = None,
) -> dict:
    """Apply the two-of-two confirmation rule to one week.

    Parameters
    ----------
    week_num : int
        Week number (1-based).
    cusum_result : dict
        Output from cusum_compute(): must contain "alert" (bool) and "S_t" (float).
    ewma_result : dict
        Output from ewma_compute(): must contain "alert" (bool) and "Z_t" (float).
    state : ConfirmationState, optional
        Persistent state across weeks. If None, a new state is created.

    Returns
    -------
    dict with keys:
        "week_num", "status", "a_t", "alert",
        "cusum_alert", "ewma_alert",
        "reason"
    """
    if state is None:
        state = ConfirmationState()

    cusum_alert = cusum_result.get("alert", False)
    ewma_alert = ewma_result.get("alert", False)

    if cusum_alert and ewma_alert:
        # CONFIRMED: both exceed
        state.record_confirmed(week_num)
        return {
            "week_num": week_num,
            "status": "CONFIRMED",
            "a_t": 1,
            "alert": True,
            "cusum_alert": True,
            "ewma_alert": True,
            "reason": (
                f"CONFIRMED: both CUSUM (S={cusum_result['S_t']:.2f} > {cusum_result.get('H_CUSUM', 5.0)}) "
                f"and EWMA (Z={ewma_result['Z_t']:.3f} > UCL={ewma_result['UCL_t']:.3f}) exceed. "
                f"a_t=1, week eligible for spatial escalation."
            ),
        }

    elif cusum_alert or ewma_alert:
        # PROVISIONAL: exactly one exceeds
        state.record_provisional(week_num)
        which = "CUSUM" if cusum_alert else "EWMA"
        return {
            "week_num": week_num,
            "status": "PROVISIONAL",
            "a_t": 0,
            "alert": False,
            "cusum_alert": cusum_alert,
            "ewma_alert": ewma_alert,
            "reason": (
                f"PROVISIONAL: only {which} exceeds its limit "
                f"(CUSUM={'ALERT' if cusum_alert else 'ok'}, "
                f"EWMA={'ALERT' if ewma_alert else 'ok'}). "
                f"No community alert; queued for investigation. a_t=0."
            ),
        }

    else:
        # IN_CONTROL: neither exceeds
        state.record_in_control(week_num)
        return {
            "week_num": week_num,
            "status": "IN_CONTROL",
            "a_t": 0,
            "alert": False,
            "cusum_alert": False,
            "ewma_alert": False,
            "reason": (
                f"IN_CONTROL: neither CUSUM nor EWMA exceeds its limit."
            ),
        }
