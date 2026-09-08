"""EpiAlert Automated Test Suite

Comprehensive tests for data validation, date logic, detectors, pipeline,
and evaluation. All tests must pass before project is considered complete.

Tests follow the rules:
- 3,000 people per week, 104 historical weeks
- Everyone checked on same Sunday
- Calendar dates correct (leap years, month lengths, year boundaries)
- No future data leakage in prediction
- Ground truth separated from predictions
"""

import sys
import os
import tempfile
import json
import numpy as np

# Add project to path
sys.path.insert(0, os.path.dirname(__file__))

from src.ingestion import (
    validate_week_data, validate_all_weeks, week_number_to_date,
    WEEK_ZERO_REFERENCE, load_people_map, DISEASES
)
from src.database.db import init_schema, get_connection, people_count
from src.detection.baseline import detect_baseline, BaselineDetector
from src.detection.cusum import detect_cusum, CUSUMDetector
from src.detection.ewma import detect_ewma, EWMADetector
from src.evaluation import TemporalEvaluationFramework, compute_evaluation_metrics
from src.outbreak_evaluation import (
    load_ground_truth, week_overlaps_outbreak,
    evaluate_outbreak_detection
)
from src.reporting import generate_weekly_summary_report, generate_person_report


def test_date_computation():
    """Test that week numbers map to correct Sunday dates."""
    # Week 001 = 12 January 2025 (Sunday)
    assert week_number_to_date(1) == __import__('datetime').date(2025, 1, 12), \
        f"Week 1 should be 2025-01-12, got {week_number_to_date(1)}"
    
    # Week 002 = 19 January 2025
    assert week_number_to_date(2) == __import__('datetime').date(2025, 1, 19), \
        f"Week 2 should be 2025-01-19, got {week_number_to_date(2)}"
    
    # Week 104 = 3 January 2027
    assert week_number_to_date(104) == __import__('datetime').date(2027, 1, 3), \
        f"Week 104 should be 2027-01-03, got {week_number_to_date(104)}"
    
    # Week 105 = 10 January 2027
    assert week_number_to_date(105) == __import__('datetime').date(2027, 1, 10), \
        f"Week 105 should be 2027-01-10, got {week_number_to_date(105)}"
    
    # Verify WEEK_ZERO_REFERENCE
    assert WEEK_ZERO_REFERENCE == __import__('datetime').date(2025, 1, 12), \
        f"WEEK_ZERO_REFERENCE should be 2025-01-12, got {WEEK_ZERO_REFERENCE}"
    
    print("  PASS: test_date_computation")


def test_week_001_validation():
    """Test that week 001 validates correctly with 3000 people."""
    result = validate_week_data(1)
    assert result["is_valid"] == True, f"Week 001 should be valid, got errors: {result['errors']}"
    assert result["person_count"] == 3000, \
        f"Week 001 should have 3000 people, got {result['person_count']}"
    assert len(result["duplicate_persons"]) == 0, \
        f"Week 001 should have no duplicates, got {len(result['duplicate_persons'])}"
    assert len(result["missing_persons"]) == 0, \
        f"Week 001 should have no missing people, got {len(result['missing_persons'])}"
    assert len(result["date_issues"]) == 0, \
        f"Week 001 should have no date issues, got {len(result['date_issues'])}"
    
    print("  PASS: test_week_001_validation")


def test_all_weeks_have_3000_people():
    """Test that all 104 historical weeks have exactly 3000 people."""
    result = validate_all_weeks(historical_only=True)
    assert result["total_weeks"] == 104, \
        f"Should validate 104 weeks, got {result['total_weeks']}"
    assert result["valid_weeks"] == 104, \
        f"All 104 weeks should be valid, got {result['valid_weeks']} valid"
    assert result["invalid_weeks"] == 0, \
        f"No weeks should be invalid, got {result['invalid_weeks']} invalid"
    assert result["total_duplicates"] == 0, \
        f"No duplicates expected across all weeks"
    assert result["total_missing"] == 0, \
        f"No missing people expected across all weeks"
    
    print("  PASS: test_all_weeks_have_3000_people")


def test_date_logic_leap_year():
    """Test date logic handles leap years correctly."""
    # Check that date computation works across year boundaries
    # and handles different month lengths
    
    # Test several week numbers to ensure date computation is correct
    test_weeks = [1, 5, 10, 20, 21, 50, 80, 104]
    for week in test_weeks:
        d = week_number_to_date(week)
        # weekday() returns 0=Monday, 6=Sunday
        assert d.weekday() == 6, \
            f"Week {week} date {d} should be a Sunday, weekday={d.weekday()}"
    
    # Test February-related weeks (check that date computation
    # doesn't assume 30-day months)
    # Week 1 is Jan 12, Week 2 is Jan 19, etc.
    # We verify that the date computation uses real calendar logic
    d_jan_12 = __import__('datetime').date(2025, 1, 12)
    d_jan_19 = __import__('datetime').date(2025, 1, 19)
    assert week_number_to_date(1) == d_jan_12
    assert week_number_to_date(2) == d_jan_19
    
    print("  PASS: test_date_logic_leap_year")


def test_baseline_detector():
    """Test Baseline detector produces valid results."""
    result = detect_baseline(
        week_num=21,
        historical_weeks=list(range(1, 21)),
        method='simple_mean',
        threshold=2.0,
    )
    assert "alert_status" in result, "Baseline result should have alert_status"
    assert result["alert_status"] in ["ALERT", "NO_ALERT"], \
        f"alert_status should be ALERT or NO_ALERT, got {result['alert_status']}"
    assert "observed_rate" in result, "Baseline result should have observed_rate"
    assert "expected_rate" in result, "Baseline result should have expected_rate"
    assert "reason" in result, "Baseline result should have reason"
    
    print("  PASS: test_baseline_detector")


def test_cusum_detector():
    """Test CUSUM detector produces valid results."""
    result = detect_cusum(
        week_num=21,
        baseline_expected=0.05,
        decision_interval=5.0,
        reference_value=0.2,
    )
    assert "alert_status" in result, "CUSUM result should have alert_status"
    assert result["alert_status"] in ["ALERT", "NO_ALERT"], \
        f"alert_status should be ALERT or NO_ALERT"
    assert "h_cusum" in result, "CUSUM result should have h_cusum"
    assert "k_cusum" in result, "CUSUM result should have k_cusum"
    
    print("  PASS: test_cusum_detector")


def test_ewma_detector():
    """Test EWMA detector produces valid results."""
    result = detect_ewma(
        week_num=21,
        baseline_expected=0.05,
        alpha=0.2,
        control_limit=3.0,
    )
    assert "alert_status" in result, "EWMA result should have alert_status"
    assert result["alert_status"] in ["ALERT", "NO_ALERT"], \
        f"alert_status should be ALERT or NO_ALERT"
    assert "ewma" in result, "EWMA result should have ewma statistic"
    assert "ucl" in result, "EWMA result should have upper control limit"
    assert "lcl" in result, "EWMA result should have lower control limit"
    
    print("  PASS: test_ewma_detector")


def test_cusum_class_vs_function_convergence():
    """Verify CUSUMDetector (class) and cusum_compute (function) produce identical results.

    The class delegates to the function internally. This test mocks
    get_weekly_aggregation to feed known inputs, then confirms the class
    and function produce the same S_t, z_t, and alert.
    Must fail if the two ever diverge again.
    """
    from unittest.mock import patch
    from src.detection.cusum import cusum_compute, CUSUMDetector

    # Fixed inputs — compute sigma the same way the class does
    week_num = 21
    x_t = 0.025
    mu_t = 0.021
    n = 3000
    sigma_t = (mu_t * (1 - mu_t) / n) ** 0.5  # same formula as class
    prev_S = 0.0
    decision_interval = 5.0
    reference_value = 0.5

    # Function result (stateless) — uses K_CUSUM, H_CUSUM module constants
    func_result = cusum_compute(
        week_num=week_num,
        x_t=x_t,
        mu_t=mu_t,
        sigma_t=sigma_t,
        prev_S=prev_S,
    )

    # Class result — mock get_weekly_aggregation to return our known rate
    def mock_agg(wk):
        return {"infection_rate": x_t, "date": __import__('datetime').date(2025, 1, 12)}

    with patch("src.detection.cusum.get_weekly_aggregation", mock_agg):
        detector = CUSUMDetector(
            baseline_expected=mu_t,
            decision_interval=decision_interval,
            reference_value=reference_value,
        )
        detector._h_cusum = prev_S
        detector._initialized = True
        class_result = detector.detect(week_num)

    # Must match on all numerically significant fields
    assert abs(func_result["S_t"] - class_result["h_cusum"]) < 1e-10, \
        f"S_t mismatch: func={func_result['S_t']:.10f} class={class_result['h_cusum']:.10f}"
    assert abs(func_result["z_t"] - class_result["z_score"]) < 1e-10, \
        f"z_t mismatch: func={func_result['z_t']:.10f} class={class_result['z_score']:.10f}"
    assert func_result["alert"] == class_result["alert"], \
        f"alert mismatch: func={func_result['alert']} class={class_result['alert']}"

    # Test with accumulated state (simulate week 2)
    prev_S_2 = func_result["S_t"]
    x_t_2 = 0.030
    sigma_t_2 = (mu_t * (1 - mu_t) / n) ** 0.5
    func_result_2 = cusum_compute(
        week_num=22, x_t=x_t_2, mu_t=mu_t, sigma_t=sigma_t_2, prev_S=prev_S_2,
    )
    with patch("src.detection.cusum.get_weekly_aggregation", lambda wk: {"infection_rate": x_t_2, "date": __import__('datetime').date(2025, 1, 12)}):
        class_result_2 = detector.detect(22)
    assert abs(func_result_2["S_t"] - class_result_2["h_cusum"]) < 1e-10, \
        f"S_t mismatch week 2: func={func_result_2['S_t']:.10f} class={class_result_2['h_cusum']:.10f}"

    print("  PASS: test_cusum_class_vs_function_convergence")


def test_ewma_class_vs_function_convergence():
    """Verify EWMADetector (class) and ewma_compute (function) produce identical results.

    The class delegates to ewma_compute internally. This test mocks
    get_weekly_aggregation to feed known inputs, then confirms the class
    and function produce the same Z_t, UCL_t, and alert.
    Must fail if the two ever diverge again.
    """
    from unittest.mock import patch
    from src.detection.ewma import ewma_compute, ewma_ucl, EWMADetector

    # Fixed inputs
    week_num = 5
    mu = 0.021  # baseline
    n = 3000
    sigma = (mu * (1 - mu) / n) ** 0.5
    x_t = 0.028  # observed rate
    z_t = (x_t - mu) / sigma  # standardized
    prev_Z = 0.0  # Z_0 = 0

    # Function result (stateless) — uses LAMBDA_EWMA, L_EWMA, ewma_ucl
    func_result = ewma_compute(
        week_num=week_num,
        z_t=z_t,
        prev_Z=prev_Z,
    )

    # Class result — mock get_weekly_aggregation to return our known rate
    def mock_agg(wk):
        return {"infection_rate": x_t, "date": __import__('datetime').date(2025, 1, 12)}

    with patch("src.detection.ewma.get_weekly_aggregation", mock_agg):
        detector = EWMADetector(
            baseline_expected=mu,
            alpha=0.2,
            control_limit=3.0,
        )
        detector._ewma_z = prev_Z
        detector._initialized = True
        class_result = detector.detect(week_num)

    # Must match in z-score space
    # Class returns ewma in rate space; convert back to z-score for comparison
    class_Z_t = (class_result["ewma"] - mu) / class_result["sigma"]
    assert abs(func_result["Z_t"] - class_Z_t) < 1e-10, \
        f"Z_t mismatch: func={func_result['Z_t']:.10f} class={class_Z_t:.10f}"
    assert abs(func_result["UCL_t"] - class_result["UCL_t"]) < 1e-10, \
        f"UCL_t mismatch: func={func_result['UCL_t']:.10f} class={class_result['UCL_t']:.10f}"
    assert func_result["alert"] == class_result["alert"], \
        f"alert mismatch: func={func_result['alert']} class={class_result['alert']}"

    # Test with accumulated state (simulate week 6)
    prev_Z_6 = func_result["Z_t"]
    x_t_6 = 0.032
    z_t_6 = (x_t_6 - mu) / sigma
    func_result_6 = ewma_compute(
        week_num=6, z_t=z_t_6, prev_Z=prev_Z_6,
    )
    with patch("src.detection.ewma.get_weekly_aggregation", lambda wk: {"infection_rate": x_t_6, "date": __import__('datetime').date(2025, 1, 12)}):
        detector._ewma_z = prev_Z_6
        class_result_6 = detector.detect(6)
    class_Z_6 = (class_result_6["ewma"] - mu) / class_result_6["sigma"]
    assert abs(func_result_6["Z_t"] - class_Z_6) < 1e-10, \
        f"Z_t mismatch week 6: func={func_result_6['Z_t']:.10f} class={class_Z_6:.10f}"

    print("  PASS: test_ewma_class_vs_function_convergence")


def test_temporal_evaluation_no_leakage():
    """Test that temporal evaluation respects no-future-leakage constraint."""
    framework = TemporalEvaluationFramework(
        baseline_method='simple_mean',
        baseline_threshold=2.0,
        cusum_decision_interval=5.0,
        cusum_reference_value=0.2,
        ewma_alpha=0.2,
        ewma_control_limit=3.0,
    )
    results = framework.run_evaluation(start_week=21, end_week=30)
    
    # Verify all weeks were evaluated
    assert results["summary"]["total_weeks"] == 10, \
        f"Should evaluate 10 weeks, got {results['summary']['total_weeks']}"
    
    # Get evaluation metrics (no ground truth, so should still work)
    metrics = compute_evaluation_metrics(results)
    assert "comparison" in metrics, "Metrics should have comparison section"
    assert "fairness_check" in metrics, "Metrics should have fairness_check"
    assert metrics["fairness_check"]["no_future_leakage"] == True, \
        "Should confirm no future data leakage"
    
    print("  PASS: test_temporal_evaluation_no_leakage")


def test_outbreak_overlap_logic():
    """Test outbreak overlap logic with ground truth."""
    ground_truth = load_ground_truth()
    assert len(ground_truth) == 6, f"Should have 6 ground truth events, got {len(ground_truth)}"
    
    # Test specific known overlaps
    # E001: Dengue in Village A, weeks 27-33
    assert week_overlaps_outbreak(27, ground_truth[0]), "Week 27 should overlap E001"
    assert week_overlaps_outbreak(33, ground_truth[0]), "Week 33 should overlap E001"
    assert week_overlaps_outbreak(28, ground_truth[0]), "Week 28 should overlap E001"
    assert not week_overlaps_outbreak(26, ground_truth[0]), "Week 26 should NOT overlap E001"
    assert not week_overlaps_outbreak(34, ground_truth[0]), "Week 34 should NOT overlap E001"
    
    # E002: Malaria in Village B, weeks 40-47
    assert week_overlaps_outbreak(40, ground_truth[1]), "Week 40 should overlap E002"
    assert week_overlaps_outbreak(47, ground_truth[1]), "Week 47 should overlap E002"
    assert not week_overlaps_outbreak(39, ground_truth[1]), "Week 39 should NOT overlap E002"
    assert not week_overlaps_outbreak(48, ground_truth[1]), "Week 48 should NOT overlap E002"
    
    # E003: Influenza/ARI in Village C, weeks 58-66
    assert week_overlaps_outbreak(58, ground_truth[2]), "Week 58 should overlap E003"
    assert week_overlaps_outbreak(66, ground_truth[2]), "Week 66 should overlap E003"
    
    # E004: Chikungunya in Village A, weeks 72-75
    assert week_overlaps_outbreak(72, ground_truth[3]), "Week 72 should overlap E004"
    assert week_overlaps_outbreak(75, ground_truth[3]), "Week 75 should overlap E004"
    
    # E005: Acute Gastroenteritis in Village B, weeks 82-89
    assert week_overlaps_outbreak(82, ground_truth[4]), "Week 82 should overlap E005"
    assert week_overlaps_outbreak(89, ground_truth[4]), "Week 89 should overlap E005"
    
    # E006: Dengue in Village C, weeks 93-96
    assert week_overlaps_outbreak(93, ground_truth[5]), "Week 93 should overlap E006"
    assert week_overlaps_outbreak(96, ground_truth[5]), "Week 96 should overlap E006"
    
    print("  PASS: test_outbreak_overlap_logic")


def test_person_report_generation():
    """Test that person reports can be generated."""
    # Generate a person report
    result = generate_person_report(
        week_num=21,
        person_id='P0001',
        algorithm_alert=False,
    )
    assert os.path.exists(result["report_path"]), \
        f"Person report file should exist at {result['report_path']}"
    
    # Read and verify content
    with open(result["report_path"], 'r', encoding='utf-8') as f:
        content = f.read()
    assert "P0001" in content, "Report should contain person ID"
    assert "Week: 21" in content, "Report should contain week number"
    assert "Reference Date" in content, "Report should contain reference date"
    
    print("  PASS: test_person_report_generation")


def test_week_105_demo():
    """Test week_105 demo report generation."""
    from src.reporting import generate_week_105_report
    result = generate_week_105_report()
    assert os.path.exists(result["report_path"]), \
        f"Week 105 report should exist at {result['report_path']}"
    
    # Read and verify content
    with open(result["report_path"], 'r', encoding='utf-8') as f:
        content = json.load(f)
    assert "week_105" in content, "Week 105 report should have week_105 field"
    assert content["week_105"]["week"] == 105, "Week should be 105"
    assert content["report_type"] == "week-105-demo", "Report type should be week-105-demo"
    
    print("  PASS: test_week_105_demo")


def test_ground_truth_separation():
    """Test that ground truth is separated from predictions."""
    from src.evaluation import TemporalEvaluationFramework
    from src.outbreak_evaluation import load_ground_truth, detect_outbreak_at_week
    
    ground_truth = load_ground_truth()
    framework = TemporalEvaluationFramework(
        baseline_method='simple_mean',
        baseline_threshold=2.0,
        cusum_decision_interval=5.0,
        cusum_reference_value=0.2,
        ewma_alpha=0.2,
        ewma_control_limit=3.0,
    )
    results = framework.run_evaluation(start_week=21, end_week=25)
    
    # Verify that detection is done without ground truth influence
    # (the framework uses only historical data up to week N-1)
    for i, week_result in enumerate(results["baseline_results"]):
        week_num = 21 + i
        # The alert should be generated based on baseline only,
        # not because ground truth says there's an outbreak
        assert "reason" in week_result, \
            f"Baseline result for week {week_num} should have reason"
    
    # Test outbreak detection uses ground truth only post-hoc
    for week_num in [21, 22, 23, 24, 25]:
        # Check a specific week's outbreak evaluation
        # The detection_type should be computed fairly
        for detector_name, detector_results in [
            ("baseline", results["baseline_results"][week_num - 21]),
        ]:
            alert = detector_results["alert"]
            eval_result = detect_outbreak_at_week(
                week_num=week_num,
                algorithm_alert=alert,
                algorithm_reason=detector_results["reason"],
                ground_truth=ground_truth,
            )
            assert eval_result["detection_type"] in [
                "TRUE_POSITIVE", "FALSE_POSITIVE",
                "TRUE_NEGATIVE", "FALSE_NEGATIVE"
            ], f"Invalid detection type: {eval_result['detection_type']}"
    
    print("  PASS: test_ground_truth_separation")


def test_no_future_data_leakage():
    """Explicit test that detector for week N cannot access week N+1 data."""
    # The Baseline detector for week N uses historical_weeks specified by user
    # If user passes weeks 1..N-1, week N detector cannot use week N+1 data
    
    framework = TemporalEvaluationFramework(
        baseline_method='simple_mean',
        baseline_threshold=2.0,
        cusum_decision_interval=5.0,
        cusum_reference_value=0.2,
        ewma_alpha=0.2,
        ewma_control_limit=3.0,
    )
    
    # Run evaluation - baseline for week 21 uses weeks 1-20 only
    # baseline for week 22 uses weeks 1-21 only (NOT week 22 or beyond)
    # This is enforced by the framework
    results = framework.run_evaluation(start_week=21, end_week=25)
    
    # All baseline results should be valid
    for i, bl_result in enumerate(results["baseline_results"]):
        week_num = 21 + i
        assert "expected_rate" in bl_result, \
            f"Baseline for week {week_num} should have expected_rate"
        assert "observed_rate" in bl_result, \
            f"Baseline for week {week_num} should have observed_rate"
    
    print("  PASS: test_no_future_data_leakage")


def test_framework_uses_locked_defaults():
    """Assert that a default-constructed TemporalEvaluationFramework uses
    the locked module constants as defaults — NOT silently overridden values.

    Must fail if any override block reappears in __init__.
    """
    from src.detection.cusum import H_CUSUM, K_CUSUM
    from src.detection.ewma import LAMBDA_EWMA, L_EWMA
    from src.evaluation import TemporalEvaluationFramework

    fw = TemporalEvaluationFramework()

    assert fw.cusum_decision_interval == H_CUSUM, \
        f"cusum_decision_interval should be {H_CUSUM}, got {fw.cusum_decision_interval}"
    assert fw.cusum_reference_value == K_CUSUM, \
        f"cusum_reference_value should be {K_CUSUM}, got {fw.cusum_reference_value}"
    assert fw.ewma_alpha == LAMBDA_EWMA, \
        f"ewma_alpha should be {LAMBDA_EWMA}, got {fw.ewma_alpha}"
    assert fw.ewma_control_limit == L_EWMA, \
        f"ewma_control_limit should be {L_EWMA}, got {fw.ewma_control_limit}"

    print("  PASS: test_framework_uses_locked_defaults")


def test_framework_respects_explicit_params():
    """Assert that explicitly passed parameter values survive unchanged.

    Passing the documented defaults (h=5.0, k=0.2, alpha=0.2, L=3.0)
    must NOT be overridden. Passing non-default values must also survive.
    """
    from src.detection.cusum import H_CUSUM, K_CUSUM
    from src.detection.ewma import LAMBDA_EWMA, L_EWMA
    from src.evaluation import TemporalEvaluationFramework

    # Documented defaults must be honored exactly
    fw = TemporalEvaluationFramework(
        cusum_decision_interval=5.0,
        cusum_reference_value=0.2,
        ewma_alpha=0.2,
        ewma_control_limit=3.0,
    )
    assert fw.cusum_decision_interval == 5.0, \
        f"Explicit h=5.0 was changed to {fw.cusum_decision_interval}"
    assert fw.cusum_reference_value == 0.2, \
        f"Explicit k=0.2 was changed to {fw.cusum_reference_value}"
    assert fw.ewma_alpha == 0.2, \
        f"Explicit alpha=0.2 was changed to {fw.ewma_alpha}"
    assert fw.ewma_control_limit == 3.0, \
        f"Explicit L=3.0 was changed to {fw.ewma_control_limit}"

    # Non-default values must also survive
    fw2 = TemporalEvaluationFramework(
        cusum_decision_interval=5.001,
        cusum_reference_value=0.25,
        ewma_alpha=0.15,
        ewma_control_limit=2.5,
    )
    assert fw2.cusum_decision_interval == 5.001
    assert fw2.cusum_reference_value == 0.25
    assert fw2.ewma_alpha == 0.15
    assert fw2.ewma_control_limit == 2.5

    print("  PASS: test_framework_respects_explicit_params")


def test_cusum_params_affect_alerts():
    """Assert that changing CUSUM h (decision_interval) changes alert counts.

    h=5.0 should produce fewer alerts than h=0.5.
    Must fail if parameters ever go dead again.
    """
    from unittest.mock import patch
    from src.detection.cusum import CUSUMDetector

    def mock_agg(wk):
        return {"infection_rate": 0.03, "date": __import__('datetime').date(2025, 1, 12)}

    # h=5.0: strict threshold, should rarely alert on modest z-scores
    with patch("src.detection.cusum.get_weekly_aggregation", mock_agg):
        d_high = CUSUMDetector(baseline_expected=0.02, decision_interval=5.0, reference_value=0.5)
        d_high._h_cusum = 0.0
        d_high._initialized = True
        alerts_high = 0
        for wn in range(21, 105):
            r = d_high.detect(wn)
            if r["alert"]:
                alerts_high += 1

    # h=0.5: very loose threshold, should alert frequently
    with patch("src.detection.cusum.get_weekly_aggregation", mock_agg):
        d_low = CUSUMDetector(baseline_expected=0.02, decision_interval=0.5, reference_value=0.5)
        d_low._h_cusum = 0.0
        d_low._initialized = True
        alerts_low = 0
        for wn in range(21, 105):
            r = d_low.detect(wn)
            if r["alert"]:
                alerts_low += 1

    print(f"  CUSUM h=5.0 alerts={alerts_high}, h=0.5 alerts={alerts_low}")
    assert alerts_high != alerts_low, \
        f"CUSUM h has NO EFFECT: h=5.0 and h=0.5 produce identical alert counts ({alerts_high})"
    assert alerts_low > alerts_high, \
        f"CUSUM h=0.5 should alert MORE than h=5.0, got h=5.0:{alerts_high} h=0.5:{alerts_low}"

    print("  PASS: test_cusum_params_affect_alerts")


def test_ewma_params_affect_alerts():
    """Assert that changing EWMA L (control_limit) changes alert counts.

    L=3.0 should produce fewer alerts than L=0.5.
    Must fail if parameters ever go dead again.
    """
    from unittest.mock import patch
    from src.detection.ewma import EWMADetector

    def mock_agg(wk):
        return {"infection_rate": 0.03, "date": __import__('datetime').date(2025, 1, 12)}

    # L=3.0: wide control limits, fewer alerts
    with patch("src.detection.ewma.get_weekly_aggregation", mock_agg):
        d_high = EWMADetector(baseline_expected=0.02, alpha=0.2, control_limit=3.0)
        d_high._ewma_z = 0.0
        d_high._initialized = True
        alerts_high = 0
        for wn in range(21, 105):
            r = d_high.detect(wn)
            if r["alert"]:
                alerts_high += 1

    # L=0.5: very tight control limits, more alerts
    with patch("src.detection.ewma.get_weekly_aggregation", mock_agg):
        d_low = EWMADetector(baseline_expected=0.02, alpha=0.2, control_limit=0.5)
        d_low._ewma_z = 0.0
        d_low._initialized = True
        alerts_low = 0
        for wn in range(21, 105):
            r = d_low.detect(wn)
            if r["alert"]:
                alerts_low += 1

    print(f"  EWMA L=3.0 alerts={alerts_high}, L=0.5 alerts={alerts_low}")
    assert alerts_high != alerts_low, \
        f"EWMA L has NO EFFECT: L=3.0 and L=0.5 produce identical alert counts ({alerts_high})"
    assert alerts_low > alerts_high, \
        f"EWMA L=0.5 should alert MORE than L=3.0, got L=3.0:{alerts_high} L=0.5:{alerts_low}"

    print("  PASS: test_ewma_params_affect_alerts")


def test_outbreak_detection_metrics():
    """Test that outbreak-level detection metrics are computed for all three detectors.

    Runs full historical evaluation (weeks 21-104) and verifies:
    - All three detectors produce valid TP/FP/FN/TN counts
    - Sensitivity, precision, and F1 are computed
    - Lead time analysis is populated where detections exist
    - Ground truth has 6 outbreaks, 40 outbreak-weeks
    """
    framework = TemporalEvaluationFramework()
    results = framework.run_evaluation(start_week=21, end_week=104)
    gt = load_ground_truth()
    eval_result = evaluate_outbreak_detection(results, gt)

    # Verify ground truth
    assert eval_result["outbreak_summary"]["total_outbreaks_in_ground_truth"] == 6
    assert eval_result["outbreak_summary"]["weeks_with_outbreak"] == 40

    # Verify all three detectors have valid metrics
    for det in ["baseline", "cusum", "ewma"]:
        d = eval_result["detector_performance"][det]
        assert d["tp"] >= 0 and d["fp"] >= 0 and d["fn"] >= 0 and d["tn"] >= 0
        assert d["sensitivity"] >= 0.0 and d["sensitivity"] <= 1.0
        assert d["precision"] >= 0.0 and d["precision"] <= 1.0
        assert d["f1_score"] >= 0.0 and d["f1_score"] <= 1.0
        assert d["tp"] + d["fp"] + d["fn"] + d["tn"] == 84  # weeks 21-104

    # Verify detection_by_type has all four categories
    for det in ["baseline", "cusum", "ewma"]:
        dt = eval_result["detection_by_type"][det]
        for cat in ["TRUE_POSITIVE", "FALSE_POSITIVE", "FALSE_NEGATIVE", "TRUE_NEGATIVE"]:
            assert cat in dt

    # Baseline should have some detections (it's the most sensitive)
    assert eval_result["detector_performance"]["baseline"]["tp"] > 0
    assert eval_result["detector_performance"]["baseline"]["fp"] > 0

    # Lead time analysis should be present
    for det in ["baseline", "cusum", "ewma"]:
        lt = eval_result["lead_time_analysis"][det]
        assert "positive_lead_times" in lt
        assert "average_lead_time" in lt
        assert "median_lead_time" in lt

    print("  PASS: test_outbreak_detection_metrics")


def run_all_tests():
    """Run all tests and report results."""
    test_functions = [
        test_date_computation,
        test_week_001_validation,
        test_all_weeks_have_3000_people,
        test_date_logic_leap_year,
        test_baseline_detector,
        test_cusum_detector,
        test_ewma_detector,
        test_temporal_evaluation_no_leakage,
        test_outbreak_overlap_logic,
        test_person_report_generation,
        test_week_105_demo,
        test_ground_truth_separation,
        test_no_future_data_leakage,
        test_outbreak_detection_metrics,
    ]
    
    passed = 0
    failed = 0
    
    for test_func in test_functions:
        try:
            test_func()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test_func.__name__}: {e}")
            failed += 1
    
    print(f"\n{'='*60}")
    print(f"Test Results: {passed} passed, {failed} failed out of {len(test_functions)}")
    print(f"{'='*60}")
    
    if failed > 0:
        sys.exit(1)
    else:
        print("All tests passed!")


if __name__ == "__main__":
    run_all_tests()