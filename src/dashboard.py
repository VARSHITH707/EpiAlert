"""EpiAlert Console Dashboard Module

Provides a lightweight console-based dashboard for the EpiAlert project.

Allows users to:
- Select a week and view surveillance statistics
- View alerts from Baseline, CUSUM, and EWMA detectors
- Compare detector performance
- Inspect outbreak status
- View reports
- Demonstrate week_105 as incoming data

The dashboard is suitable for conference demonstration.
"""

import sys
import os
import json
import argparse
from pathlib import Path

# Add project to path
sys.path.insert(0, os.path.dirname(__file__))

from src.evaluation import TemporalEvaluationFramework
from src.detection.baseline import detect_baseline
from src.detection.cusum import detect_cusum
from src.detection.ewma import detect_ewma
from src.reporting import (
    generate_weekly_summary_report,
    generate_person_report,
    generate_detector_comparison_report,
    generate_evaluation_results_report,
    generate_week_105_report,
)
from src.outbreak_evaluation import load_ground_truth
from src.database.db import get_connection


class EPIDashboard:
    """Console-based dashboard for EpiAlert."""

    def __init__(self):
        self.framework = None
        self.current_week = None
        self.ground_truth = load_ground_truth()
        self.evaluation_results = None

    def clear(self):
        """Clear the console screen."""
        os.system('cls' if os.name == 'nt' else 'clear')

    def print_header(self, title):
        """Print a formatted dashboard header."""
        self.clear()
        print("=" * 70)
        print(f"  EpiAlert - Infectious Disease Outbreak Early-Warning System")
        print("=" * 70)
        print(f"\n  {title}")
        print("-" * 70)

    def show_week_selection(self):
        """Show week selection menu."""
        print("\n  Select a week to view surveillance data:")
        print(f"    [1-20]  Baseline initialization weeks (1-20)")
        print(f"    [21-104] Detection/testing weeks (21-104)")
        print(f"    [105]   Week 105 - Live/Demo mode (new incoming data)")
        print(f"    [exit]  Exit the dashboard")
        print("-" * 70)

    def show_week_data(self, week_num):
        """Show data for a specific week."""
        from src.preprocessing import get_weekly_aggregation

        # Get weekly aggregation
        agg = get_weekly_aggregation(week_num)

        # Get framework results if available
        if self.framework and self.current_week == week_num:
            bl_result = self.framework.baseline_results[week_num - 21] if week_num >= 21 else None
            cu_result = self.framework.cusum_results[week_num - 21] if week_num >= 21 else None
            ew_result = self.framework.ewma_results[week_num - 21] if week_num >= 21 else None
        else:
            bl_result = cu_result = ew_result = None

        # Compute detectors for this week if not already done
        if bl_result is None:
            bl_result = detect_baseline(
                week_num=week_num,
                historical_weeks=list(range(1, max(week_num, 21))),
                method='simple_mean',
                threshold=2.0,
            )

        if cu_result is None:
            cu_result = detect_cusum(
                week_num=week_num,
                baseline_expected=bl_result["expected_rate"],
                decision_interval=5.0,
                reference_value=0.2,
            )

        if ew_result is None:
            ew_result = detect_ewma(
                week_num=week_num,
                baseline_expected=bl_result["expected_rate"],
                alpha=0.2,
                control_limit=3.0,
            )

        # Get reference date
        from src.ingestion import week_number_to_date
        ref_date = week_number_to_date(week_num)

        # Check outbreak status
        outbreak_active = False
        active_outbreaks = []
        for outbreak in self.ground_truth:
            # Simple check: is this week within outbreak period?
            # Outbreak weeks are 1-based
            if outbreak["start_week"] <= week_num <= outbreak["end_week"]:
                outbreak_active = True
                active_outbreaks.append(outbreak["event_id"])

        # Disease distribution
        disease_dist = agg["disease_counts"]
        total_infected = sum(1 for p in agg["people"] if p["infected"])

        # Display the dashboard
        self.print_header(f"Week {week_num:3d} - {ref_date.strftime('%B %d, %Y')} (Sunday)")

        # Infection statistics
        print(f"\n  Weekly Surveillance Statistics:")
        print(f"  ━" * 60)
        print(f"  Total people in week:    {agg['total_people']:4d}")
        print(f"  People infected:         {total_infected:4d} ({total_infected/agg['total_people']*100:5.1f}%)")
        print(f"  Infection rate:          {agg['infection_rate']:6.4f}")
        print(f"\n  Disease distribution:")
        for disease, count in sorted(disease_dist.items()):
            if count > 0:
                print(f"    {disease:25s}: {count:3d} ({count/agg['total_people']*100:4.1f}%)")

        # Outbreak status
        print(f"\n  Outbreak Detection:")
        print(f"  ━" * 60)
        if outbreak_active:
            print(f"  ⚠  OUTBREAK ACTIVE this week!")
            print(f"  Active outbreak events: {', '.join(active_outbreaks)}")
        else:
            print(f"  ✓  No active outbreaks this week")

        # Detector results
        print(f"\n  Detection Algorithm Results:")
        print(f"  ━" * 60)

        # Baseline
        bl_alert = "🔴 ALERT" if bl_result["alert"] else "🟢 NO_ALERT"
        print(f"  Baseline:    {bl_alert:12s} (rate: {bl_result['observed_rate']:6.4f} vs expected: {bl_result['expected_rate']:6.4f})")

        # CUSUM
        cu_alert = "🔴 ALERT" if cu_result["alert"] else "🟢 NO_ALERT"
        print(f"  CUSUM:       {cu_alert:12s} (h-cusum: {cu_result['h_cusum']:6.4f})")

        # EWMA
        ew_alert = "🔴 ALERT" if ew_result["alert"] else "🟢 NO_ALERT"
        print(f"  EWMA:        {ew_alert:12s} (EWMA: {ew_result['ewma']:6.4f} vs UCL: {ew_result['ucl']:6.4f})")

        # Alert summary
        any_alert = bl_result["alert"] or cu_result["alert"] or ew_result["alert"]
        print(f"  {'='*60}")
        if any_alert:
            print(f"  SUMMARY: At least one detector raised an ALERT this week")
        else:
            print(f"  SUMMARY: No alerts raised by any detector this week")
        print(f"  {'='*60}")

        # Navigation options
        print(f"\n  Navigation:")
        if week_num > 1:
            print(f"    [p] Previous week (week {week_num - 1})")
        if week_num < 105:
            print(f"    [n] Next week (week {week_num + 1})")
        print(f"    [r] Generate reports for this week")
        print(f"    [d] Dashboard menu")
        print(f"    [e] Exit")

    def run(self):
        """Run the dashboard main loop."""
        print("  Welcome to EpiAlert Dashboard")
        print("  " + "=" * 60)

        # Main menu loop
        while True:
            self.show_week_selection()
            choice = input("\n  Enter your choice: ").strip().lower()

            if choice == 'exit':
                print("  Thank you for using EpiAlert!")
                break

            elif choice == 'help':
                print("""
  EpiAlert Dashboard Help:
  
  - Select a week number (1-105) to view surveillance data
  - Weeks 1-20: Baseline initialization period
  - Weeks 21-104: Detection/testing period
  - Week 105: Live/demo mode with new incoming data
  
  Dashboard features:
   - Weekly infection statistics
   - Outbreak detection status
   - Three detector comparisons (Baseline, CUSUM, EWMA)
   - Report generation
   - Week 105 demonstration
  
  Use arrow keys or number keys to navigate.
  """)
                input("\n  Press Enter to continue...")

            elif choice == 'r':
                # Generate reports for selected week
                if self.current_week is None:
                    print("  Please select a week first!")
                    input("  Press Enter to continue...")
                    continue

                week_num = self.current_week
                print(f"\n  Generating reports for week {week_num}...")

                # Generate weekly summary
                try:
                    weekly_result = generate_weekly_summary_report(
                        week_num=week_num,
                        baseline_result=self.framework.baseline_results[week_num - 21]
                        if week_num >= 21 else {},
                        cusum_result=self.framework.cusum_results[week_num - 21]
                        if week_num >= 21 else {},
                        ewma_result=self.framework.ewma_results[week_num - 21]
                        if week_num >= 21 else {},
                        ground_truth=self.ground_truth,
                    )
                    print(f"    ✓ Weekly summary: {weekly_result['report_path']}")
                except Exception as e:
                    print(f"    ✗ Weekly summary error: {e}")

                # Generate person report for sample person
                try:
                    person_result = generate_person_report(
                        week_num=week_num,
                        person_id='P0001',
                        algorithm_alert=self.framework.baseline_results[week_num - 21]["alert"]
                        if week_num >= 21 else False,
                    )
                    print(f"    ✓ Person report: {person_result['report_path']}")
                except Exception as e:
                    print(f"    ✗ Person report error: {e}")

                # Generate comparison report
                try:
                    comp_result = generate_detector_comparison_report(
                        self.framework.evaluation_results if self.framework else {},
                        self.ground_truth,
                    )
                    print(f"    ✓ Comparison report: {comp_result['report_path']}")
                except Exception as e:
                    print(f"    ✗ Comparison report error: {e}")

                input("\n  Press Enter to continue...")

            elif choice == 'd':
                # Dashboard menu
                while True:
                    self.print_header("EpiAlert Main Dashboard Menu")
                    print("  1. Select week to analyze")
                    print("  2. Week 105 - Live/Demo mode")
                    print("  3. Generate all reports")
                    print("  4. View evaluation results")
                    print("  5. Back to week selection")
                    print("  6. Exit")
                    print("-" * 70)

                    sub_choice = input("\n  Enter choice: ").strip()

                    if sub_choice == '1':
                        try:
                            week = int(input("  Enter week number (1-105): "))
                            if 1 <= week <= 105:
                                self.current_week = week
                                self.show_week_data(week)
                                input("\n  Press Enter to continue...")
                            else:
                                print("  Invalid week number!")
                                input("  Press Enter to continue...")
                        except ValueError:
                            print("  Please enter a valid number!")
                            input("  Press Enter to continue...")

                    elif sub_choice == '2':
                        # Week 105 demo
                        self.current_week = 105
                        w105_result = generate_week_105_report()
                        print(f"\n  Week 105 demo report: {w105_result['report_path']}")
                        # Show week 105 data
                        from src.preprocessing import get_weekly_aggregation
                        agg = get_weekly_aggregation(105)
                        print(f"\n  Week 105 Statistics:")
                        print(f"    Total people: {agg['total_people']}")
                        print(f"    Infection rate: {agg['infection_rate']:.4f}")
                        input("\n  Press Enter to continue...")

                    elif sub_choice == '3':
                        # Generate all reports
                        if self.current_week is None or self.current_week < 21:
                            print("  Please select a historical week (21-104) first!")
                            input("  Press Enter to continue...")
                            continue

                        week_num = self.current_week
                        print(f"\n  Generating all reports for week {week_num}...")

                        try:
                            weekly_result = generate_weekly_summary_report(
                                week_num=week_num,
                                baseline_result=self.framework.baseline_results[week_num - 21],
                                cusum_result=self.framework.cusum_results[week_num - 21],
                                ewma_result=self.framework.ewma_results[week_num - 21],
                                ground_truth=self.ground_truth,
                            )
                            print(f"    ✓ Weekly summary generated")
                        except Exception as e:
                            print(f"    ✗ Weekly summary error: {e}")

                        try:
                            person_result = generate_person_report(
                                week_num=week_num,
                                person_id='P0001',
                                algorithm_alert=self.framework.baseline_results[week_num - 21]["alert"],
                            )
                            print(f"    ✓ Person report generated")
                        except Exception as e:
                            print(f"    ✗ Person report error: {e}")

                        try:
                            comp_result = generate_detector_comparison_report(
                                self.framework.__dict__ if self.framework else {},
                                self.ground_truth,
                            )
                            print(f"    ✓ Comparison report generated")
                        except Exception as e:
                            print(f"    ✗ Comparison report error: {e}")

                        input("\n  Press Enter to continue...")

                    elif sub_choice == '4':
                        # View evaluation results
                        if self.framework is None or self.evaluation_results is None:
                            # Run evaluation
                            self.framework = TemporalEvaluationFramework(
                                baseline_method='simple_mean',
                                baseline_threshold=2.0,
                                cusum_decision_interval=5.0,
                                cusum_reference_value=0.2,
                                ewma_alpha=0.2,
                                ewma_control_limit=3.0,
                            )
                            self.evaluation_results = self.framework.run_evaluation(
                                start_week=21, end_week=104
                            )

                        try:
                            eval_result = generate_evaluation_results_report(
                                self.evaluation_results, self.ground_truth
                            )
                            print(f"\n  Evaluation results: {eval_result['report_path']}")
                        except Exception as e:
                            print(f"  ✗ Evaluation results error: {e}")

                        input("\n  Press Enter to continue...")

                    elif sub_choice == '5':
                        break

                    elif sub_choice == '6':
                        print("  Thank you for using EpiAlert!")
                        return

                    else:
                        print("  Invalid choice!")
                        input("  Press Enter to continue...")

            elif choice == 'n':
                # Next week
                if self.current_week is not None and self.current_week < 105:
                    self.current_week += 1
                    self.show_week_data(self.current_week)
                    input("\n  Press Enter to continue...")
                else:
                    print("  Already at week 105!")
                    input("  Press Enter to continue...")

            elif choice == 'p':
                # Previous week
                if self.current_week is not None and self.current_week > 1:
                    self.current_week -= 1
                    self.show_week_data(self.current_week)
                    input("\n  Press Enter to continue...")
                else:
                    print("  At week 1!")
                    input("  Press Enter to continue...")

            else:
                print("  Invalid choice! Please try again.")
                input("  Press Enter to continue...")


def main():
    """Entry point for the dashboard."""
    dashboard = EPIDashboard()
    dashboard.run()


if __name__ == "__main__":
    main()