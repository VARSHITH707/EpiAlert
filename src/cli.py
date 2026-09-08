"""EpiAlert CLI Commands

Provides command-line interface for operating the EpiAlert project.

Commands:
1. validating the dataset
2. running historical evaluation
3. running a specific week
4. running week_105 demo
5. generating reports
6. generating evaluation results
7. running tests
8. starting the dashboard
"""

import sys
import os
import argparse
import json

# Add project to path
sys.path.insert(0, os.path.dirname(__file__))

from src.ingestion import validate_week_data, validate_all_weeks, ingest_all_historical, ingest_week_105
from src.evaluation import TemporalEvaluationFramework, compute_evaluation_metrics
from src.database.db import init_schema, get_connection, people_count, get_weekly_stats
from src.alerts.service import AlertService, SMSDispatchService, MockSMSProvider, ProductionSMSProvider
from src.reporting import (
    generate_weekly_summary_report,
    generate_person_report,
    generate_detector_comparison_report,
    generate_evaluation_results_report,
    generate_week_105_report,
)
from src.outbreak_evaluation import load_ground_truth
from src.dashboard import EPIDashboard


def cmd_validate(args):
    """Validate the dataset."""
    if args.week is not None:
        result = validate_week_data(args.week)
        print(f"Validating week {args.week:03d}:")
        print(f"  Status: {'VALID' if result['is_valid'] else 'INVALID'}")
        print(f"  People: {result['person_count']}")
        if result["duplicate_persons"]:
            print(f"  Duplicates: {len(result['duplicate_persons'])}")
            for dp in result["duplicate_persons"][:3]:
                print(f"    - {dp}")
        if result["missing_persons"]:
            print(f"  Missing: {len(result['missing_persons'])} people")
        if result["date_issues"]:
            print(f"  Date issues: {len(result['date_issues'])}")
        if result["warnings"]:
            print(f"  Warnings: {len(result['warnings'])}")
        for w in result["warnings"][:3]:
            print(f"    - {w}")
    else:
        result = validate_all_weeks()
        print(f"Validating all {result['total_weeks']} historical weeks:")
        print(f"  Valid: {result['valid_weeks']}, Invalid: {result['invalid_weeks']}")
        print(f"  Total duplicates: {result['total_duplicates']}")
        print(f"  Total missing: {result['total_missing']}")
        print(f"  Total date issues: {result['total_date_issues']}")
        if result["invalid_weeks"] > 0:
            print(f"  !! {result['invalid_weeks']} weeks had errors")


def cmd_ingest_all(args):
    """Ingest every available week of consolidated reports."""
    print("Ingesting all available weeks...")
    result = ingest_all_historical()
    weeks = result["weeks"]
    print(f"Weeks ingested: {result['total_weeks']}"
          f" (week {weeks[0]} to {weeks[-1]})" if weeks else "Weeks ingested: 0")
    print(f"Reports inserted: {result['total_inserted']:,}")
    print(f"Quarantined: {result['total_quarantined']:,}")

    # Flag weeks that came up short. The expected size is taken from the people
    # table rather than assumed, so a smaller dataset does not produce a page
    # of spurious warnings.
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM people")
    expected = cur.fetchone()[0]
    for week in weeks:
        cur.execute("SELECT COUNT(*) FROM reports WHERE week_number = ?", (week,))
        count = cur.fetchone()[0]
        if count != expected:
            print(f"  Week {week:3d}: {count:5,} reports (expected {expected:,})")
    conn.close()


def cmd_evaluate(args):
    """Run temporal evaluation."""
    print(f"Running evaluation from week {args.start_week} to {args.end_week}...")
    
    framework = TemporalEvaluationFramework(
        baseline_method=args.baseline_method,
        baseline_threshold=args.baseline_threshold,
        cusum_decision_interval=args.cusum_decision_interval,
        cusum_reference_value=args.cusum_reference_value,
        ewma_alpha=args.ewma_alpha,
        ewma_control_limit=args.ewma_control_limit,
    )
    
    results = framework.run_evaluation(start_week=args.start_week, end_week=args.end_week)
    
    # Print summary
    print(f"\nEvaluation Results:")
    print(f"  Weeks evaluated: {results['summary']['total_weeks']}")
    print(f"  Baseline alerts: {results['summary']['baseline_alerts']}")
    print(f"  CUSUM alerts: {results['summary']['cusum_alerts']}")
    print(f"  EWMA alerts: {results['summary']['ewma_alerts']}")
    
    # Compute metrics
    metrics = compute_evaluation_metrics(results)
    print(f"\nComparison (alert rates):")
    for detector, m in metrics["comparison"].items():
        print(f"  {detector}: {m['total_alerted']} alerts out of {m['total_evaluated']} weeks (rate: {m['alert_rate']:.4f})")
    
    # Save results
    if args.output:
        with open(args.output, 'w') as f:
            json.dump(results, f, indent=2)
        print(f"\nResults saved to: {args.output}")


def cmd_reports(args):
    """Generate reports."""
    framework = None
    ground_truth = None
    
    if args.evaluate:
        # Load evaluation results
        if args.evaluate == "new":
            framework = TemporalEvaluationFramework(
                baseline_method=args.baseline_method,
                baseline_threshold=args.baseline_threshold,
                cusum_decision_interval=args.cusum_decision_interval,
                cusum_reference_value=args.cusum_reference_value,
                ewma_alpha=args.ewma_alpha,
                ewma_control_limit=args.ewma_control_limit,
            )
            results = framework.run_evaluation(start_week=args.start_week, end_week=args.end_week)
        else:
            # Load existing
            with open(args.evaluate) as f:
                results = json.load(f)
    
    if args.ground_truth:
        ground_truth = load_ground_truth()
    
    if args.report_type == "weekly":
        # Generate weekly summary
        week_num = args.week
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM reports WHERE week_number = ?", (week_num,))
        row = cur.fetchone()
        conn.close()
        
        if row:
            # Get baseline result from framework if available
            if framework and args.start_week <= week_num <= args.end_week:
                idx = week_num - args.start_week
                bl_result = framework.baseline_results[idx] if idx < len(framework.baseline_results) else {}
                cu_result = framework.cusum_results[idx] if idx < len(framework.cusum_results) else {}
                ew_result = framework.ewma_results[idx] if idx < len(framework.ewma_results) else {}
            else:
                # Compute on the fly
                from src.detection.baseline import detect_baseline
                bl_result = detect_baseline(
                    week_num=week_num,
                    historical_weeks=list(range(1, week_num)),
                    method=args.baseline_method,
                    threshold=args.baseline_threshold,
                )
                from src.detection.cusum import detect_cusum
                cu_result = detect_cusum(
                    week_num=week_num,
                    baseline_expected=bl_result["expected_rate"],
                    decision_interval=args.cusum_decision_interval,
                    reference_value=args.cusum_reference_value,
                )
                from src.detection.ewma import detect_ewma
                ew_result = detect_ewma(
                    week_num=week_num,
                    baseline_expected=bl_result["expected_rate"],
                    alpha=args.ewma_alpha,
                    control_limit=args.ewma_control_limit,
                )
        else:
            bl_result = cu_result = ew_result = {}
            
        generate_weekly_summary_report(
            week_num=week_num,
            baseline_result=bl_result,
            cusum_result=cu_result,
            ewma_result=ew_result,
            ground_truth=ground_truth,
        )
        print(f"Weekly summary report generated for week {week_num}")
    
    elif args.report_type == "person":
        # Generate person report
        generate_person_report(
            week_num=args.week,
            person_id=args.person_id,
            algorithm_alert=args.alert,
        )
        print(f"Person report generated for person {args.person_id}, week {args.week}")
    
    elif args.report_type == "comparison":
        generate_detector_comparison_report(results, ground_truth)
        print("Detector comparison report generated")
    
    elif args.report_type == "evaluation":
        generate_evaluation_results_report(results, ground_truth)
        print("Evaluation results report generated")
    
    elif args.report_type == "week_105":
        generate_week_105_report()
        print("Week 105 demo report generated")


def cmd_alerts(args):
    """Generate alerts from detection_results."""
    svc = AlertService()
    
    if args.both:
        for mode in ['confirmation', 'union']:
            n = svc.generate_alerts(fusion_mode=mode, min_status=args.min_status, use_ollama=not args.no_ollama)
            print(f"  {mode}: {n} alert rows")
    else:
        print(f"Generating alerts from detection_results (fusion_mode={args.fusion_mode})...")
        n = svc.generate_alerts(
            fusion_mode=args.fusion_mode,
            min_status=args.min_status,
            use_ollama=not args.no_ollama,
        )
        print(f"  Created/updated {n} alert rows")
    
    # Show a real sample message from the DB
    alerts = svc.list_alerts(status="ALERT", limit=3)
    if alerts:
        print(f"\n  Sample alert messages ({len(alerts)} shown):")
        for a in alerts:
            print(f"    [{a['fusion_mode']}] {a['disease']}, {a['village']}, {a['street']}, week {a['week_number']}")
            print(f"      message: {a['message']}")
            if a.get('explanation'):
                print(f"      explanation: {a['explanation'][:80]}...")
            print(f"      sms_eligible: {a['sms_eligible']}")
            print()


def cmd_detect(args):
    """Run street-level detection and write to detection_results.

    This is the detection that matters: per (disease, village, street) rather
    than whole-population. `evaluate` runs the population-level comparison and
    does not populate detection_results, so an install that only ran `evaluate`
    has no per-street results and cannot raise alerts.
    """
    from src.evaluation_spatial import run_spatial_evaluation

    print(f"Detecting weeks {args.start_week}-{args.end_week} "
          f"(fusion_mode={args.fusion_mode})...")

    result = run_spatial_evaluation(
        start_week=args.start_week,
        end_week=args.end_week,
        disease=None,          # all diseases, not just the default one
        fusion_mode=args.fusion_mode,
    )

    print(f"  spatial units : {len(result.get('spatial_units', []))}")
    print(f"  rows written  : {result.get('total_rows', 0):,}")
    summary = result.get("summary", {})
    for status, n in summary.items():
        if n:
            print(f"    {status:14s} {n:>7,}")

    if not result.get("total_rows"):
        print("\n  No results produced. Check that reports were ingested.")
        sys.exit(1)


def cmd_createuser(args):
    """Create a login for the web application.

    The password is prompted for when not supplied, so it does not end up in
    shell history. It is never stored in plain text -- only a bcrypt hash.
    """
    import getpass

    from src.web.auth import create_user

    password = args.password
    if not password:
        password = getpass.getpass(f"Password for {args.username}: ")
        if password != getpass.getpass("Repeat password: "):
            print("Passwords did not match.")
            sys.exit(1)

    if len(password) < 8:
        print("Password must be at least 8 characters.")
        sys.exit(1)

    try:
        user_id = create_user(args.username, password)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    print(f"Created user {args.username!r} (id={user_id}).")
    print("Start the site with:")
    print("  .venv\\Scripts\\python.exe -m uvicorn src.web.main:app --reload")


def cmd_sms(args):
    """Send SMS for sms_eligible alerts."""
    import os
    
    # Choose provider based on SMS_MODE env var (default: mock)
    sms_mode = os.getenv("SMS_MODE", "mock").lower()
    if sms_mode == "live":
        provider = ProductionSMSProvider()
    else:
        provider = MockSMSProvider()
        if sms_mode != "mock":
            print(f"WARNING: SMS_MODE={sms_mode!r} not recognised, falling back to mock.")
    
    print(f"SMS provider: {provider.name} (SMS_MODE={sms_mode or 'mock'})")
    
    svc = AlertService()
    dispatch = SMSDispatchService(svc, provider=provider)
    
    if args.alert_id:
        # Dispatch a single alert by ID
        alert = svc.get_alert(args.alert_id)
        if not alert:
            print(f"Error: no alert with id={args.alert_id}")
            sys.exit(1)
        if not alert.get("sms_eligible"):
            print(f"Alert {args.alert_id} is not sms_eligible.")
            sys.exit(1)
        print(f"Dispatching alert {args.alert_id}: {alert['disease']}, {alert['village']}, {alert['street']}, week {alert['week_number']}")
        results = dispatch.dispatch_alert(alert)
        sent = sum(1 for r in results if r["delivery_status"] == "sent")
        skipped = sum(1 for r in results if r["delivery_status"] == "skipped")
        failed = len(results) - sent - skipped
        print(f"  Sent {sent}, skipped {skipped} (already delivered), failed {failed}:")
        for r in results:
            print(f"    {r['phone_number']}: {r['delivery_status']} (sms_id={r['sms_id']})")
    else:
        # Dispatch ALL sms_eligible alerts
        print("Dispatching SMS for all sms_eligible alerts...")
        summary = dispatch.dispatch_all_sms_eligible()
        print(f"  Provider: {summary['provider']}")
        print(f"  Alerts dispatched: {summary['alerts_dispatched']}")
        print(f"  SMS sent: {summary['sms_sent']}")
        print(f"  SMS skipped (already delivered): {summary['sms_skipped']}")
        print(f"  SMS failed: {summary['sms_failed']}")
        print(f"  Messages by provider: {summary['by_provider']}")
    
    # Report sms_messages table state (full count, not just first 50)
    from src.alerts.service import get_sms_messages, get_distinct_phone_numbers
    all_rows = get_sms_messages(limit=10000)
    phones = get_distinct_phone_numbers()
    total_sms = len(all_rows)
    print(f"\n  sms_messages table: {total_sms} rows total, {len(phones)} distinct phone numbers")
    if all_rows:
        print(f"  Distinct phone numbers used: {sorted(set(r['phone_number'] for r in all_rows))}")
        max_per_alert = max((sum(1 for r in all_rows if r['alert_id'] == aid) for aid in set(r['alert_id'] for r in all_rows)), default=0)
        print(f"  Max sms_messages per alert: {max_per_alert}")
        print(f"  Sample rows (first 3):")
        for r in all_rows[:3]:
            pn = r['phone_number']
            print(f"    sms_id={r['sms_id']} alert_id={r['alert_id']} phone={pn} status={r['delivery_status']} provider={r['provider']}")
    
    # Idempotency: dispatch again and report delta
    print(f"\n  Re-running SMS dispatch to test idempotency...")
    summary2 = dispatch.dispatch_all_sms_eligible()
    print(f"  Second run: {summary2['sms_sent']} SMS sent, {summary2['sms_failed']} failed")
    all_rows2 = get_sms_messages(limit=10000)
    print(f"  sms_messages rows after second run: {len(all_rows2)} (delta: {len(all_rows2) - total_sms})")



def cmd_dashboard(args):
    """Start the dashboard."""
    dashboard = EPIDashboard()
    dashboard.run()


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog='epialert',
        description='EpiAlert - Infectious Disease Outbreak Early-Warning System'
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # validate command
    validate_parser = subparsers.add_parser('validate', help='Validate the dataset')
    validate_parser.add_argument('--week', type=int, default=None,
                                  help='Specific week to validate (1-105)')
    
    # ingest-all command
    ingest_parser = subparsers.add_parser('ingest-all', help='Ingest all 104 historical weeks')
    
    # evaluate command
    evaluate_parser = subparsers.add_parser('evaluate', help='Run temporal evaluation')
    evaluate_parser.add_argument('--start-week', type=int, default=21,
                                  help='First week to evaluate (default: 21)')
    evaluate_parser.add_argument('--end-week', type=int, default=104,
                                  help='Last week to evaluate (default: 104)')
    evaluate_parser.add_argument('--baseline-method', choices=['simple_mean', 'moving_average'],
                                  default='simple_mean', help='Baseline calculation method')
    evaluate_parser.add_argument('--baseline-threshold', type=float, default=2.0,
                                  help='Alert threshold in standard deviations')
    evaluate_parser.add_argument('--cusum-decision-interval', type=float, default=5.0,
                                  help='CUSUM decision threshold (h parameter)')
    evaluate_parser.add_argument('--cusum-reference-value', type=float, default=0.2,
                                  help='CUSUM reference value (k parameter)')
    evaluate_parser.add_argument('--ewma-alpha', type=float, default=0.2,
                                  help='EWMA smoothing parameter (0 < alpha <= 1)')
    evaluate_parser.add_argument('--ewma-control-limit', type=float, default=3.0,
                                  help='EWMA control limit multiplier (L parameter)')
    evaluate_parser.add_argument('--output', type=str, default=None,
                                  help='Save results JSON to file')
    
    # reports command
    reports_parser = subparsers.add_parser('reports', help='Generate reports')
    reports_parser.add_argument('--report-type', choices=['weekly', 'person', 'comparison', 'evaluation', 'week_105'],
                                  required=True, help='Type of report to generate')
    reports_parser.add_argument('--week', type=int, default=None,
                                 help='Week number (for weekly/person reports)')
    reports_parser.add_argument('--person-id', type=str, default=None,
                                  help='Person ID (for person reports)')
    reports_parser.add_argument('--alert', action='store_true', default=False,
                                  help='Whether algorithm raised alert (for person report)')
    reports_parser.add_argument('--evaluate', type=str, default=None,
                                  help='Load existing evaluation results JSON file')
    reports_parser.add_argument('--ground-truth', action='store_true', default=False,
                                  help='Include ground truth in outbreak analysis')
    reports_parser.add_argument('--start-week', type=int, default=21,
                                  help='Start week for evaluation')
    reports_parser.add_argument('--end-week', type=int, default=104,
                                  help='End week for evaluation')
    reports_parser.add_argument('--baseline-method', choices=['simple_mean', 'moving_average'],
                                  default='simple_mean')
    reports_parser.add_argument('--baseline-threshold', type=float, default=2.0)
    reports_parser.add_argument('--cusum-decision-interval', type=float, default=5.0)
    reports_parser.add_argument('--cusum-reference-value', type=float, default=0.2)
    reports_parser.add_argument('--ewma-alpha', type=float, default=0.2)
    reports_parser.add_argument('--ewma-control-limit', type=float, default=3.0)
    
    # evaluate-as-subcommand of reports
    # (already handled by --report-type and --evaluate flags)
    
    # alerts command
    alerts_parser = subparsers.add_parser('alerts', help='Generate alerts from detection_results')
    alerts_parser.add_argument('--fusion-mode', choices=['union', 'confirmation'], default='confirmation',
                              help='Which detection_results rows to read (default: confirmation)')
    alerts_parser.add_argument('--min-status', choices=['WATCH', 'ALERT', 'HIGH_ALERT'], default='ALERT',
                              help='Minimum status to convert to an alert')
    alerts_parser.add_argument('--no-ollama', action='store_true', default=False,
                              help='Use only the deterministic template, not Ollama')
    alerts_parser.add_argument('--both', action='store_true', default=False,
                              help='Generate alerts for both union and confirmation modes')
    
    # sms command
    sms_parser = subparsers.add_parser('sms', help='Send SMS for sms_eligible alerts')
    sms_parser.add_argument('--alert-id', type=int, default=None,
                            help='Dispatch SMS for a single alert by ID')
    
    # dashboard command
    detect_parser = subparsers.add_parser(
        'detect', help='Run street-level detection (fills detection_results)')
    detect_parser.add_argument('--start-week', type=int, default=21)
    detect_parser.add_argument('--end-week', type=int, default=104)
    detect_parser.add_argument(
        '--fusion-mode', default='confirmation',
        choices=['confirmation', 'union'],
        help='confirmation requires CUSUM and EWMA to agree (default)')

    createuser_parser = subparsers.add_parser(
        'createuser', help='Create a web application login')
    createuser_parser.add_argument(
        '--username', required=True, help='Username for the new account')
    createuser_parser.add_argument(
        '--password', default=None,
        help='Password. Omit to be prompted, which keeps it out of shell history.')

    dashboard_parser = subparsers.add_parser('dashboard', help='Start the dashboard')
    
    args = parser.parse_args()
    
    if args.command == 'validate':
        cmd_validate(args)
    elif args.command == 'ingest-all':
        cmd_ingest_all(args)
    elif args.command == 'evaluate':
        cmd_evaluate(args)
    elif args.command == 'reports':
        cmd_reports(args)
    elif args.command == 'alerts':
        cmd_alerts(args)
    elif args.command == 'sms':
        cmd_sms(args)
    elif args.command == 'detect':
        cmd_detect(args)
    elif args.command == 'createuser':
        cmd_createuser(args)
    elif args.command == 'dashboard':
        cmd_dashboard(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()