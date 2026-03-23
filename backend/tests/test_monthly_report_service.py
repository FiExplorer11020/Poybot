from app.services.monthly_report_service import (
    example_monthly_payload,
    generate_monthly_report_pdf,
    performance_verdict,
)


def test_performance_verdict_thresholds() -> None:
    assert performance_verdict(2.41).label == "Excellent"
    assert performance_verdict(1.5).label == "Bon"
    assert performance_verdict(0.7).label == "Passable"
    assert performance_verdict(0.2).label == "Mauvais"


def test_generate_monthly_report_pdf(tmp_path) -> None:
    payload = example_monthly_payload()
    output_path = tmp_path / "monthly-report.pdf"

    generate_monthly_report_pdf(payload, output_path)

    content = output_path.read_bytes()
    assert output_path.exists()
    assert content.startswith(b"%PDF")
    assert len(content) > 8_000
