import base64
from app.services.export_service import generate_excel, generate_pdf

# 1x1 transparent PNG base64 representation
MOCK_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

def test_generate_excel_with_meta():
    title = "Test Excel Report"
    headers = ["Name", "Value"]
    rows = [
        {"Name": "Item A", "Value": 100},
        {"Name": "Item B", "Value": 200}
    ]
    summary = "This is a **mock markdown** summary text.\n- Bullet 1\n- Bullet 2"
    kpis = [
        {"label": "Total Value", "value": "$300", "change": "+10%"},
        {"label": "Average", "value": "$150", "change": "Neutral"}
    ]
    
    # Generate Excel bytes
    excel_bytes = generate_excel(
        title=title,
        headers=headers,
        rows=rows,
        summary=summary,
        kpis=kpis
    )
    
    assert isinstance(excel_bytes, bytes)
    assert len(excel_bytes) > 0


def test_generate_pdf_with_meta():
    title = "Test PDF Report"
    headers = ["Name", "Value"]
    rows = [
        {"Name": "Item A", "Value": 100},
        {"Name": "Item B", "Value": 200}
    ]
    summary = "This is a **mock markdown** summary text.\n- Bullet 1\n- Bullet 2"
    kpis = [
        {"label": "Total Value", "value": "$300", "change": "+10%"},
        {"label": "Average", "value": "$150", "change": "Neutral"}
    ]
    chart_image = MOCK_PNG_BASE64

    # Generate PDF bytes
    pdf_bytes = generate_pdf(
        title=title,
        headers=headers,
        rows=rows,
        summary=summary,
        chart_image=chart_image,
        kpis=kpis
    )
    
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 0


def test_generate_pdf_without_meta():
    title = "Test PDF Report Minimal"
    headers = ["Name", "Value"]
    rows = [
        {"Name": "Item A", "Value": 100}
    ]
    
    # Generate PDF bytes
    pdf_bytes = generate_pdf(
        title=title,
        headers=headers,
        rows=rows
    )
    
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 0
