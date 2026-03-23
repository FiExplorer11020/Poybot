from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import xml.etree.ElementTree as ET

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS = "http://purl.org/dc/elements/1.1/"
DCTERMS_NS = "http://purl.org/dc/terms/"
DCMITYPE_NS = "http://purl.org/dc/dcmitype/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"

PAGE_WIDTH = 11906  # A4
PAGE_HEIGHT = 16838
MARGIN = 1440
CONTENT_WIDTH = PAGE_WIDTH - (MARGIN * 2)

COLOR_NAVY = "0F172A"
COLOR_GOLD = "C6A970"
COLOR_STEEL = "334155"
COLOR_LIGHT = "F8FAFC"
COLOR_MID = "E2E8F0"
COLOR_TEXT = "111827"
COLOR_MUTED = "475569"
COLOR_CODE = "0B1220"
COLOR_CODE_TEXT = "E2E8F0"
COLOR_SUCCESS = "14532D"
COLOR_WARNING = "92400E"
COLOR_DANGER = "7F1D1D"


def xml_document(body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>{body}'


def needs_preserve(text: str) -> bool:
    return text.startswith(" ") or text.endswith(" ") or "  " in text


def run(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    color: str | None = None,
    size: int | None = None,
    font: str | None = None,
) -> str:
    props: list[str] = []
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    if color:
        props.append(f'<w:color w:val="{color}"/>')
    if size:
        props.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    if font:
        props.append(
            f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:eastAsia="{font}" w:cs="{font}"/>'
        )
    r_pr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
    space = ' xml:space="preserve"' if needs_preserve(text) else ""
    return f"<w:r>{r_pr}<w:t{space}>{escape(text)}</w:t></w:r>"


def instr_text(text: str) -> str:
    return f'<w:r><w:instrText xml:space="preserve">{escape(text)}</w:instrText></w:r>'


def fld_char(kind: str) -> str:
    return f'<w:r><w:fldChar w:fldCharType="{kind}"/></w:r>'


def simple_field(instr: str, placeholder: str) -> str:
    return (
        f'<w:fldSimple w:instr="{escape(instr)}">'
        f'{run(placeholder, color=COLOR_MUTED)}'
        "</w:fldSimple>"
    )


def paragraph(
    runs: list[str] | str,
    *,
    style: str | None = None,
    align: str | None = None,
    page_break_before: bool = False,
    spacing_before: int | None = None,
    spacing_after: int | None = None,
    keep_next: bool = False,
    keep_lines: bool = False,
    border_bottom: str | None = None,
    indent_left: int | None = None,
) -> str:
    if isinstance(runs, str):
        runs = [run(runs)]

    p_style = f'<w:pStyle w:val="{style}"/>' if style else ""
    keep_bits = ""
    if keep_next:
        keep_bits += "<w:keepNext/>"
    if keep_lines:
        keep_bits += "<w:keepLines/>"
    page_break = "<w:pageBreakBefore/>" if page_break_before else ""
    border_bits = ""
    if border_bottom:
        border_bits = (
            "<w:pBdr>"
            f'<w:bottom w:val="single" w:sz="10" w:space="4" w:color="{border_bottom}"/>'
            "</w:pBdr>"
        )
    spacing_bits = ""
    if spacing_before is not None or spacing_after is not None:
        before = spacing_before if spacing_before is not None else 0
        after = spacing_after if spacing_after is not None else 0
        spacing_bits = f'<w:spacing w:before="{before}" w:after="{after}"/>'
    indent_bits = f'<w:ind w:left="{indent_left}"/>' if indent_left is not None else ""
    align_bits = f'<w:jc w:val="{align}"/>' if align else ""
    props = f"{p_style}{keep_bits}{page_break}{border_bits}{spacing_bits}{indent_bits}{align_bits}"
    p_pr = f"<w:pPr>{props}</w:pPr>" if props else ""
    return f"<w:p>{p_pr}{''.join(runs)}</w:p>"


def blank_paragraph() -> str:
    return paragraph([run("")], spacing_after=80)


def heading(text: str, level: int, *, page_break_before: bool = False) -> str:
    style = {1: "Heading1", 2: "Heading2", 3: "Heading3"}[level]
    return paragraph(
        [run(text)],
        style=style,
        page_break_before=page_break_before,
        keep_next=True,
        spacing_before=240 if level == 1 else 120,
        spacing_after=120,
    )


def cell_paragraphs(
    content: str | list[str],
    *,
    bold: bool = False,
    color: str | None = None,
    style: str | None = None,
    align: str | None = None,
) -> list[str]:
    if isinstance(content, list):
        return [paragraph([run(item, bold=bold, color=color)], style=style, align=align) for item in content]
    return [paragraph([run(content, bold=bold, color=color)], style=style, align=align)]


def table_cell(
    paragraphs_xml: list[str],
    *,
    width: int,
    fill: str | None = None,
    color: str | None = None,
    align: str | None = None,
) -> str:
    if align:
        paragraphs_xml = [
            paragraph(
                [run(extract_text(p), color=color)],
                align=align,
            )
            if p.startswith("<w:p>") and "<w:pPr>" not in p
            else p
            for p in paragraphs_xml
        ]
    borders = (
        f'<w:top w:val="single" w:sz="6" w:color="{COLOR_MID}"/>'
        f'<w:left w:val="single" w:sz="6" w:color="{COLOR_MID}"/>'
        f'<w:bottom w:val="single" w:sz="6" w:color="{COLOR_MID}"/>'
        f'<w:right w:val="single" w:sz="6" w:color="{COLOR_MID}"/>'
    )
    shading = f'<w:shd w:val="clear" w:fill="{fill}"/>' if fill else ""
    return (
        "<w:tc>"
        "<w:tcPr>"
        f'<w:tcW w:w="{width}" w:type="dxa"/>'
        f"<w:tcBorders>{borders}</w:tcBorders>"
        f"{shading}"
        "<w:tcMar>"
        '<w:top w:w="90" w:type="dxa"/>'
        '<w:left w:w="120" w:type="dxa"/>'
        '<w:bottom w:w="90" w:type="dxa"/>'
        '<w:right w:w="120" w:type="dxa"/>'
        "</w:tcMar>"
        '<w:vAlign w:val="center"/>'
        "</w:tcPr>"
        f"{''.join(paragraphs_xml)}"
        "</w:tc>"
    )


def extract_text(paragraph_xml: str) -> str:
    try:
        root = ET.fromstring(paragraph_xml.replace("w:", "{%s}" % W_NS))
    except ET.ParseError:
        return ""
    texts = []
    for node in root.iter():
        if node.tag.endswith("}t") and node.text:
            texts.append(node.text)
    return "".join(texts)


def table(
    headers: list[str],
    rows: list[list[str]],
    widths: list[int],
    *,
    header_fill: str = COLOR_NAVY,
    stripe_fill: str = "F8FAFC",
) -> str:
    if len(headers) != len(widths):
        raise ValueError("headers/widths mismatch")

    grid = "".join(f'<w:gridCol w:w="{width}"/>' for width in widths)
    header_cells = "".join(
        table_cell(
            cell_paragraphs(text, bold=True, color=COLOR_LIGHT, align="center"),
            width=widths[idx],
            fill=header_fill,
        )
        for idx, text in enumerate(headers)
    )
    rows_xml = [f"<w:tr>{header_cells}</w:tr>"]

    for row_idx, row in enumerate(rows):
        fill = stripe_fill if row_idx % 2 == 0 else None
        cells = "".join(
            table_cell(
                cell_paragraphs(text, color=COLOR_TEXT),
                width=widths[idx],
                fill=fill,
            )
            for idx, text in enumerate(row)
        )
        rows_xml.append(f"<w:tr>{cells}</w:tr>")

    return (
        "<w:tbl>"
        "<w:tblPr>"
        f'<w:tblW w:w="{CONTENT_WIDTH}" w:type="dxa"/>'
        '<w:tblLayout w:type="fixed"/>'
        "</w:tblPr>"
        f"<w:tblGrid>{grid}</w:tblGrid>"
        f"{''.join(rows_xml)}"
        "</w:tbl>"
    )


def code_block(text: str) -> str:
    lines = text.strip("\n").splitlines()
    paragraphs_xml = [
        paragraph([run(line if line else " ", font="Consolas", size=19, color=COLOR_CODE_TEXT)], style="CodeBlock")
        for line in lines
    ]
    return (
        "<w:tbl>"
        "<w:tblPr>"
        f'<w:tblW w:w="{CONTENT_WIDTH}" w:type="dxa"/>'
        '<w:tblLayout w:type="fixed"/>'
        "</w:tblPr>"
        f'<w:tblGrid><w:gridCol w:w="{CONTENT_WIDTH}"/></w:tblGrid>'
        "<w:tr>"
        f'{table_cell(paragraphs_xml, width=CONTENT_WIDTH, fill=COLOR_CODE)}'
        "</w:tr>"
        "</w:tbl>"
    )


def metadata_table(metadata: list[tuple[str, str]]) -> str:
    rows = [[label, value] for label, value in metadata]
    return table(
        ["Champ", "Valeur"],
        rows,
        [2500, CONTENT_WIDTH - 2500],
        header_fill=COLOR_STEEL,
        stripe_fill="F8FAFC",
    )


def banner_table(title: str, subtitle: str) -> str:
    paragraphs_xml = [
        paragraph([run("POYBOT", bold=True, color=COLOR_GOLD, size=44)], align="center", spacing_after=40),
        paragraph([run(title, bold=True, color=COLOR_LIGHT, size=34)], align="center", spacing_after=80),
        paragraph([run(subtitle, color="CBD5E1", size=22)], align="center"),
    ]
    return (
        "<w:tbl>"
        "<w:tblPr>"
        f'<w:tblW w:w="{CONTENT_WIDTH}" w:type="dxa"/>'
        '<w:tblLayout w:type="fixed"/>'
        "</w:tblPr>"
        f'<w:tblGrid><w:gridCol w:w="{CONTENT_WIDTH}"/></w:tblGrid>'
        "<w:tr>"
        f'{table_cell(paragraphs_xml, width=CONTENT_WIDTH, fill=COLOR_NAVY)}'
        "</w:tr>"
        "</w:tbl>"
    )


def core_properties(author: str, title: str) -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return xml_document(
        f"""
        <cp:coreProperties
            xmlns:cp="{CP_NS}"
            xmlns:dc="{DC_NS}"
            xmlns:dcterms="{DCTERMS_NS}"
            xmlns:dcmitype="{DCMITYPE_NS}"
            xmlns:xsi="{XSI_NS}">
          <dc:title>{escape(title)}</dc:title>
          <dc:creator>{escape(author)}</dc:creator>
          <cp:lastModifiedBy>{escape(author)}</cp:lastModifiedBy>
          <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
          <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
        </cp:coreProperties>
        """.strip()
    )


def app_properties() -> str:
    return xml_document(
        f"""
        <Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
                    xmlns:vt="{VT_NS}">
          <Application>Codex OOXML Generator</Application>
          <DocSecurity>0</DocSecurity>
          <ScaleCrop>false</ScaleCrop>
          <HeadingPairs>
            <vt:vector size="2" baseType="variant">
              <vt:variant><vt:lpstr>Title</vt:lpstr></vt:variant>
              <vt:variant><vt:i4>1</vt:i4></vt:variant>
            </vt:vector>
          </HeadingPairs>
          <TitlesOfParts>
            <vt:vector size="1" baseType="lpstr">
              <vt:lpstr>Poybot Specification</vt:lpstr>
            </vt:vector>
          </TitlesOfParts>
          <Company>OpenAI</Company>
          <LinksUpToDate>false</LinksUpToDate>
          <SharedDoc>false</SharedDoc>
          <HyperlinksChanged>false</HyperlinksChanged>
          <AppVersion>1.0</AppVersion>
        </Properties>
        """.strip()
    )


def content_types() -> str:
    return xml_document(
        """
        <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
          <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
          <Default Extension="xml" ContentType="application/xml"/>
          <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
          <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
          <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
          <Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
          <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
          <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
        </Types>
        """.strip()
    )


def package_relationships() -> str:
    return xml_document(
        """
        <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
          <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
          <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
        </Relationships>
        """.strip()
    )


def document_relationships() -> str:
    return xml_document(
        """
        <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
          <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>
          <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
        </Relationships>
        """.strip()
    )


def settings_xml() -> str:
    return xml_document(
        f"""
        <w:settings xmlns:w="{W_NS}">
          <w:updateFields w:val="true"/>
        </w:settings>
        """.strip()
    )


def styles_xml() -> str:
    return xml_document(
        f"""
        <w:styles xmlns:w="{W_NS}">
          <w:docDefaults>
            <w:rPrDefault>
              <w:rPr>
                <w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/>
                <w:sz w:val="22"/>
                <w:szCs w:val="22"/>
                <w:color w:val="{COLOR_TEXT}"/>
                <w:lang w:val="fr-FR"/>
              </w:rPr>
            </w:rPrDefault>
            <w:pPrDefault>
              <w:pPr>
                <w:spacing w:after="90" w:line="276" w:lineRule="auto"/>
              </w:pPr>
            </w:pPrDefault>
          </w:docDefaults>
          <w:latentStyles w:defLockedState="0" w:defUIPriority="99" w:defSemiHidden="0" w:defUnhideWhenUsed="0" w:defQFormat="0" w:count="276"/>

          <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
            <w:name w:val="Normal"/>
            <w:qFormat/>
          </w:style>

          <w:style w:type="paragraph" w:styleId="Title">
            <w:name w:val="Title"/>
            <w:basedOn w:val="Normal"/>
            <w:qFormat/>
            <w:pPr><w:spacing w:before="120" w:after="120"/><w:jc w:val="center"/></w:pPr>
            <w:rPr>
              <w:b/>
              <w:color w:val="{COLOR_NAVY}"/>
              <w:sz w:val="36"/>
              <w:szCs w:val="36"/>
            </w:rPr>
          </w:style>

          <w:style w:type="paragraph" w:styleId="Subtitle">
            <w:name w:val="Subtitle"/>
            <w:basedOn w:val="Normal"/>
            <w:qFormat/>
            <w:pPr><w:spacing w:after="120"/><w:jc w:val="center"/></w:pPr>
            <w:rPr>
              <w:color w:val="{COLOR_MUTED}"/>
              <w:sz w:val="24"/>
              <w:szCs w:val="24"/>
            </w:rPr>
          </w:style>

          <w:style w:type="paragraph" w:styleId="Heading1">
            <w:name w:val="heading 1"/>
            <w:basedOn w:val="Normal"/>
            <w:next w:val="Normal"/>
            <w:qFormat/>
            <w:pPr>
              <w:keepNext/>
              <w:keepLines/>
              <w:spacing w:before="240" w:after="120"/>
            </w:pPr>
            <w:rPr>
              <w:b/>
              <w:color w:val="{COLOR_NAVY}"/>
              <w:sz w:val="30"/>
              <w:szCs w:val="30"/>
            </w:rPr>
          </w:style>

          <w:style w:type="paragraph" w:styleId="Heading2">
            <w:name w:val="heading 2"/>
            <w:basedOn w:val="Normal"/>
            <w:next w:val="Normal"/>
            <w:qFormat/>
            <w:pPr>
              <w:keepNext/>
              <w:keepLines/>
              <w:spacing w:before="180" w:after="90"/>
            </w:pPr>
            <w:rPr>
              <w:b/>
              <w:color w:val="{COLOR_STEEL}"/>
              <w:sz w:val="26"/>
              <w:szCs w:val="26"/>
            </w:rPr>
          </w:style>

          <w:style w:type="paragraph" w:styleId="Heading3">
            <w:name w:val="heading 3"/>
            <w:basedOn w:val="Normal"/>
            <w:next w:val="Normal"/>
            <w:qFormat/>
            <w:pPr>
              <w:keepNext/>
              <w:keepLines/>
              <w:spacing w:before="120" w:after="60"/>
            </w:pPr>
            <w:rPr>
              <w:b/>
              <w:color w:val="{COLOR_STEEL}"/>
              <w:sz w:val="24"/>
              <w:szCs w:val="24"/>
            </w:rPr>
          </w:style>

          <w:style w:type="paragraph" w:styleId="TOCHeading">
            <w:name w:val="TOC Heading"/>
            <w:basedOn w:val="Normal"/>
            <w:qFormat/>
            <w:pPr><w:spacing w:before="120" w:after="120"/></w:pPr>
            <w:rPr><w:b/><w:color w:val="{COLOR_NAVY}"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr>
          </w:style>

          <w:style w:type="paragraph" w:styleId="CodeBlock">
            <w:name w:val="Code Block"/>
            <w:basedOn w:val="Normal"/>
            <w:pPr>
              <w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/>
              <w:ind w:left="180"/>
            </w:pPr>
            <w:rPr>
              <w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:eastAsia="Consolas" w:cs="Consolas"/>
              <w:color w:val="{COLOR_CODE_TEXT}"/>
              <w:sz w:val="19"/>
              <w:szCs w:val="19"/>
            </w:rPr>
          </w:style>
        </w:styles>
        """.strip()
    )


def footer_xml() -> str:
    return xml_document(
        f"""
        <w:ftr xmlns:w="{W_NS}">
          <w:p>
            <w:pPr>
              <w:jc w:val="center"/>
            </w:pPr>
            <w:r>
              <w:rPr><w:color w:val="{COLOR_MUTED}"/><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>
              <w:t>Poybot — Spécification Technique v1.0 | Page </w:t>
            </w:r>
            <w:fldSimple w:instr=" PAGE ">
              <w:r>
                <w:rPr><w:color w:val="{COLOR_MUTED}"/><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>
                <w:t>1</w:t>
              </w:r>
            </w:fldSimple>
          </w:p>
        </w:ftr>
        """.strip()
    )


def document_xml(author: str, status: str, report_date: str) -> str:
    body: list[str] = []

    body.append(blank_paragraph())
    body.append(
        banner_table(
            "Poybot — Spécification Technique v1.0",
            "Bot Polymarket orienté latency arbitrage et orchestration multi-stratégies",
        )
    )
    body.append(blank_paragraph())
    body.append(
        paragraph(
            [run("Document de référence architecture / trading / exploitation", italic=True, color=COLOR_MUTED)],
            style="Subtitle",
        )
    )
    body.append(blank_paragraph())
    body.append(
        metadata_table(
            [
                ("Date", report_date),
                ("Auteur", author),
                ("Version", "v1.0"),
                ("Statut", status),
                ("Base d’analyse", "Dépôt Poybot + configuration runtime + audit réseau + contrats WebSocket officiels"),
            ]
        )
    )
    body.append(blank_paragraph())
    body.append(
        paragraph(
            [
                run("Portée"),
                run(" : "),
                run(
                    "ce document décrit l’architecture actuelle du dépôt et l’architecture cible courte échéance. "
                    "Les modules non branchés au runtime principal sont identifiés explicitement."
                ),
            ],
            border_bottom=COLOR_GOLD,
            spacing_after=180,
        )
    )
    body.append(paragraph([run("")], page_break_before=True))

    body.append(paragraph([run("Table des matières")], style="TOCHeading"))
    body.append(
        paragraph(
            [
                fld_char("begin"),
                instr_text(' TOC \\o "1-3" \\h \\z \\u '),
                fld_char("separate"),
                run("La table des matières se mettra à jour à l’ouverture dans Word si nécessaire.", italic=True, color=COLOR_MUTED),
                fld_char("end"),
            ],
            spacing_after=180,
        )
    )

    body.append(heading("1. Vue d'ensemble", 1, page_break_before=True))
    body.append(
        paragraph(
            "Poybot est une plateforme de market-intelligence et d’exécution Polymarket conçue pour capter des dislocations de prix très courtes sur des marchés binaires. Le cœur du système combine une acquisition temps réel des quotes, une couche de signal, des garde-fous de risque, un pont d’exécution et un tableau de bord Next.js consommant un état live centralisé."
        )
    )
    body.append(
        paragraph(
            "L’objectif produit est le latency arbitrage crypto sur Polymarket: comparer un prix binaire YES/NO ou sa probabilité implicite avec un oracle spot plus rapide, puis déclencher un ordre uniquement si l’avantage net dépasse les coûts, la staleness, la cohérence YES+NO et les plafonds de portefeuille."
        )
    )
    body.append(
        table(
            ["Bloc", "Technologie", "Rôle opérationnel"],
            [
                ["API / runtime", "FastAPI + Uvicorn", "Exposition REST, WebSocket `/ws/live`, orchestration du `LiveHub` et tick loop à 250 ms."],
                ["Persistance", "PostgreSQL + SQLAlchemy + Alembic", "Stockage des métadonnées Gamma, order books, trades, snapshots portefeuille et statuts de jobs."],
                ["Queue / scheduling", "Redis + ARQ", "Exécution des crons de sync métadonnées et backfill trades récents."],
                ["Cache live", "PriceStateCache en mémoire", "Accélération lecture top-of-book pour le runtime et réduction de latence applicative."],
                ["Frontend", "Next.js 15 + React 18 + Zustand", "Dashboard live, commandes bot, visualisation marchés, PnL et historique trades."],
                ["Infra locale", "Docker Compose", "Assemblage `api`, `worker`, `frontend`, `postgres`, `redis`, `clickhouse`."],
            ],
            [1500, 2300, CONTENT_WIDTH - 3800],
        )
    )
    body.append(
        table(
            ["Mode", "Comportement", "Usage recommandé"],
            [
                ["`dry_run`", "Aucun ordre réel; `TradeExecutor` renvoie un `order_id` simulé et un statut `SIMULATED`.", "Validation end-to-end, UI, tests d’intégration, calibration signaux."],
                ["`live`", "Envoi d’ordres vers l’endpoint CLOB configuré si `polymarket_trading_enabled=true` et credentials présents.", "Uniquement après hardening sécurité, monitoring et contrôles de risque validés."],
            ],
            [1400, 3600, CONTENT_WIDTH - 5000],
        )
    )
    body.append(
        paragraph(
            "Le runtime actuel utilise majoritairement la stratégie adaptative branchée dans `LiveHub`. Les moteurs `LatencyArbEngine` et `SpreadArbScanner` sont présents, testés unitairement, mais non encore injectés dans la boucle de décision principale."
        )
    )

    body.append(heading("2. Architecture système", 1, page_break_before=True))
    body.append(
        paragraph(
            "Le système suit une architecture en pipeline avec un point de vérité opérationnel: `backend/app/live/state.py`. Ce module agrège le cache prix, la stratégie active, le risk guard, l’exécuteur et la diffusion WebSocket vers le frontend."
        )
    )
    body.append(
        code_block(
            """
[Binance Spot WS - cible oracle rapide] ----+
                                            |
[Polymarket Gamma REST] ----> [Universe Builder / Market Sync] ----+
                                                                    |
[Polymarket CLOB WS / REST] --> [Ingestion persistante + PriceStateCache] --> [LiveHub]
                                                                                  |
                                                                                  v
        +------------------- Signal Layer -------------------+          +----------------------+
        | AdaptiveStrategyEngine (actif runtime)            |          | RiskGuard            |
        | LatencyArbEngine (module prêt, non branché)       | -------> | Drawdown/API halt    |
        | SpreadArbScanner (module prêt, non branché)       |          | Exposure / cooldown  |
        +---------------------------------------------------+          +----------+-----------+
                                                                                  |
                                                                                  v
                                                                      [TradeExecutor]
                                                                                  |
                                                                                  v
                                                              Dry-run simulator / CLOB live order
                                                                                  |
                                                                                  v
                                             REST `/api/v1/live-summary` + WS `/ws/live` + Next.js dashboard
            """.strip()
        )
    )
    body.append(
        table(
            ["Couche", "Modules principaux", "Entrées", "Sorties / contrat"],
            [
                ["1. Ingestion", "`GammaClient`, `ClobClient`, `PolymarketWsIngestor`, `PriceStateCache`, `UniverseBuilder`", "REST Gamma, REST CLOB, WS CLOB", "Univers marchés, top-of-book persisté, cache live, messages bruts."],
                ["2. Signal", "`AdaptiveStrategyEngine`, `LatencyArbEngine`, `SpreadArbScanner`", "Quotes YES/NO, spot oracle, historique court", "Edge attendu, direction, seuil d’entrée, confiance."],
                ["3. Risk", "`RiskConfig`, `RiskGuard`, classification de régime", "Portfolio, edge, erreurs API, drawdown", "Notional autorisé, kill switches, stop broadcast."],
                ["4. Execution", "`TradeExecutor`, routes live", "Signal qualifié + ordre logique", "Dry-run simulé ou ordre CLOB réel, état trade."],
                ["5. Analytics", "`LiveHub.snapshot()`, `/portfolio/pnl-by-timeframe`, dashboard", "Trades, historique, stats, snapshots", "KPIs, série PnL, visualisation live, monitoring opérateur."],
            ],
            [1200, 2800, 1600, CONTENT_WIDTH - 5600],
        )
    )
    body.append(
        table(
            ["Interdépendance", "Description"],
            [
                ["Ingestion -> Signal", "Le signal ne lit pas directement le réseau; il consomme des quotes normalisées et datées, idéalement issues du `PriceStateCache`."],
                ["Signal -> Risk", "Aucun ordre n’est possible sans sizing `size_position()` puis validation `RiskGuard` drawdown/API."],
                ["Risk -> Execution", "Le halt drawdown/API coupe le bot, annule les ordres ouverts et émet un message `halt` consommable côté UI."],
                ["Execution -> Analytics", "Chaque trade enrichit le snapshot live, la table `recent_trades`, le PnL cumulé et l’historique portefeuille."],
                ["Analytics -> Frontend", "Le frontend bootstrap via `/api/v1/live-summary` puis passe en mode streaming via `/ws/live`."],
            ],
            [2100, CONTENT_WIDTH - 2100],
        )
    )
    body.append(
        paragraph(
            "Deux chemins d’ingestion coexistent actuellement: un chemin persistant (`PolymarketWsIngestor`) qui écrit `TopOfBook` et un chemin runtime (`LiveHub._listen_to_clob`) qui maintient l’état opérateur. Cette dualité explique une partie du drift actuel et justifie une convergence future vers un contrat unique."
        )
    )

    body.append(heading("3. Stratégies de trading", 1, page_break_before=True))
    body.append(
        table(
            ["Stratégie", "Statut d’implémentation", "Finalité"],
            [
                ["Latency Arbitrage", "Module backend présent, tests unitaires, non branché au `LiveHub`", "Détecter un retard de repricing Polymarket par rapport à un spot crypto rapide."],
                ["Spread Arbitrage", "Scanner présent, tests unitaires, non branché au `LiveHub`", "Capturer les dislocations `YES + NO < 1.0` après frais."],
                ["Adaptive Strategy", "Actif dans le runtime principal", "Suivre des micro-mouvements avec seuils dynamiques, régime de marché et sizing Kelly contraint."],
            ],
            [1800, 2500, CONTENT_WIDTH - 4300],
        )
    )

    body.append(heading("3.1 Latency Arbitrage", 2))
    body.append(
        paragraph(
            "Le moteur `LatencyArbEngine` vise les marchés de type strike/expiry (ex. “BTC above $105,000 by 3pm?”). Il extrait le strike depuis le titre, calcule le temps restant avant expiration, compare la probabilité “fair” dérivée du spot au mid Polymarket, puis déclenche `BUY_YES` ou `BUY_NO` si l’écart dépasse un plancher strict."
        )
    )
    body.append(
        code_block(
            """
T_d = time_to_expiry_h / 24
sigma_T = vol_daily * sqrt(T_d)
distance_pct = (spot_mid - strike) / strike
z = distance_pct / sigma_T
fair_prob = Phi(z)
poly_mid = (best_bid + best_ask) / 2

if fair_prob > poly_mid:
    direction = BUY_YES
    edge = fair_prob - poly_mid
else:
    direction = BUY_NO
    edge = poly_mid - fair_prob

confidence =
    min(1, edge / confidence_edge_scale)
  * max(0, 1 - poly_spread / confidence_spread_scale)
  * max(0, 1 - spot_age_ms / max_spot_age_ms)
            """.strip()
        )
    )
    body.append(
        table(
            ["Paramètre", "Défaut", "Plage recommandée", "Notes"],
            [
                ["`min_edge`", "0.04", "0.01 - 0.10", "Seuil minimal d’edge absolu avant déclenchement."],
                ["`min_time_to_expiry_h`", "0.25 h", "0.10 - 1 h", "Évite les marchés trop proches de l’expiration."],
                ["`max_time_to_expiry_h`", "24 h", "6 - 48 h", "Reste sur un horizon court, cohérent avec le vol paramétrée."],
                ["`max_poly_spread`", "0.06", "0.01 - 0.08", "Empêche d’acheter une inefficience mangée par le spread."],
                ["`max_spot_age_ms`", "500 ms", "100 - 1000 ms", "Exige un oracle spot très frais."],
                ["`vol_daily`", "0.04", "0.02 - 0.10", "Volatilité quotidienne utilisée dans la formule log-normale simplifiée."],
                ["`spot_symbol`", "`BTCUSDT`", "Mapping spot / marché", "Le moteur suppose aujourd’hui un oracle spot unique."],
                ["`confidence_edge_scale`", "0.10", "0.05 - 0.20", "Normalisation de l’edge dans la métrique de confiance."],
                ["`confidence_spread_scale`", "0.08", "0.03 - 0.10", "Pénalise les books Polymarket trop larges."],
            ],
            [1800, 1200, 1700, CONTENT_WIDTH - 4700],
        )
    )
    body.append(
        paragraph(
            "Critères de rejet implémentés: strike introuvable, marché expiré, âge spot trop élevé, spread Polymarket trop large, horizon hors fenêtre et edge inférieur au minimum. Cette stratégie est prête pour intégration mais nécessite une source spot WS dédiée."
        )
    )

    body.append(heading("3.2 Spread Arbitrage", 2))
    body.append(
        paragraph(
            "Le scanner `SpreadArbScanner` opère sur un marché binaire unique. Il compare l’ask YES et l’ask NO d’un même market, recherche un coût cumulé inférieur à 1.0, puis retire les frais aller-retour avant de classer les opportunités."
        )
    )
    body.append(
        code_block(
            """
combined_cost = ask_yes + ask_no
gross_profit = 1.0 - combined_cost
fees = combined_cost * (fee_bps / 10000) * 2
net_profit = gross_profit - fees
net_profit_pct = (net_profit / combined_cost) * 100
max_size_usdc = min(liquidity_yes, liquidity_no)

signal valid if:
    combined_cost < 1.0
    net_profit > MIN_NET_PROFIT
    max_size_usdc > 0
            """.strip()
        )
    )
    body.append(
        table(
            ["Paramètre / contrainte", "Défaut", "Plage recommandée", "Notes"],
            [
                ["`MIN_NET_PROFIT`", "0.002", "0.001 - 0.01", "Profit net minimal par lot binaire après frais."],
                ["`fee_bps`", "8 bps", "4 - 20 bps", "Frais aller-retour calculés deux fois sur le coût cumulé."],
                ["`ask_yes`, `ask_no`", "N/A", "(0, 1)", "Quotes top-of-book nécessaires sur les deux branches."],
                ["`combined_cost`", "N/A", "< 1.0", "Condition d’existence d’un spread arb brut."],
                ["`max_size_usdc`", "N/A", "> 0", "Borné par la plus faible liquidité disponible."],
            ],
            [2300, 1100, 1700, CONTENT_WIDTH - 5100],
        )
    )
    body.append(
        paragraph(
            "Dans le jeu de tests fourni, un exemple `0.42 + 0.55 = 0.97` génère un profit brut de `0.03` et un profit net d’environ `0.028448`, soit `2.93%` du capital immobilisé. Le scanner trie ensuite les opportunités par `net_profit_pct` décroissant."
        )
    )

    body.append(heading("3.3 Adaptive Strategy", 2))
    body.append(
        paragraph(
            "La stratégie adaptative est la stratégie active. Elle traite chaque marché comme une courte série de mid-prices, mesure momentum, volatilité réalisée, coût de trading et qualité de liquidité, puis produit un edge attendu et un seuil d’entrée dynamique modulé par le régime de marché."
        )
    )
    body.append(
        code_block(
            """
spread = best_ask - best_bid
mid = clip((best_bid + best_ask) / 2)
momentum = mid_t - mid_0
volatility = rolling_std(diff(mid_series))

liquidity_score = max(0, 1 - spread / spread_cap)
trading_cost = spread + fee_bps / 10000
raw_edge = max(0, abs(momentum) - trading_cost - 0.4 * volatility)
expected_edge = raw_edge * liquidity_score

entry_threshold =
    max(base_entry_threshold, 1.1 * trading_cost + 0.3 * volatility)
    * regime_multiplier

signal_strength = expected_edge / entry_threshold
direction = BUY_YES if momentum >= 0 else BUY_NO

detected if:
    observations >= min_observations
    spread <= spread_cap
    signal_strength >= min_signal_strength
    regime != CRISIS
            """.strip()
        )
    )
    body.append(
        code_block(
            """
risk_cap = equity * risk_per_trade_pct
exposure_cap = equity * max_total_exposure_pct - capital_in_trade
edge_scale = clamp(expected_edge / base_entry_threshold, 0, 1)
kelly_scale = clamp(kelly_fraction * edge_scale, 0, 1)
notional = min(risk_cap, exposure_cap) * kelly_scale
            """.strip()
        )
    )
    body.append(
        table(
            ["Paramètre signal", "Défaut", "Plage recommandée", "Effet"],
            [
                ["`base_entry_threshold`", "0.005", "0.002 - 0.02", "Plancher absolu du seuil d’entrée."],
                ["`spread_cap`", "0.06", "0.01 - 0.08", "Bloque les books trop larges."],
                ["`fee_bps`", "8", "4 - 20", "Rentre dans le coût de trading et le PnL round-trip."],
                ["`min_observations`", "4", "3 - 12", "Historique minimal avant détection."],
                ["`min_signal_strength`", "1.0", "1.0 - 2.0", "Rapport edge / threshold minimal."],
                ["`lookback`", "20", "10 - 60", "Fenêtre de la série mid utilisée pour momentum / vol."],
                ["`signal_staleness_seconds`", "3 s", "1 - 10 s", "Au-delà, le marché est considéré stale."],
                ["`cooldown_seconds`", "10 s", "5 - 60 s", "Évite la réentrée immédiate sur un marché récemment traité."],
            ],
            [2000, 1000, 1700, CONTENT_WIDTH - 4700],
        )
    )
    body.append(
        table(
            ["Paramètre allocation", "Défaut", "Plage recommandée", "Effet"],
            [
                ["`risk_per_trade_pct`", "1%", "0.25% - 2%", "Cap nominal par trade."],
                ["`max_total_exposure_pct`", "25%", "10% - 40%", "Cap global portefeuille engagé."],
                ["`kelly_fraction`", "25%", "10% - 50%", "Fraction de Kelly appliquée à l’edge normalisé."],
                ["`allocation_mode`", "`automatic`", "`automatic` / `manual`", "Mode sizing adaptatif ou ticket fixe."],
                ["`manual_notional_amount`", "100 USDC", "10 - 500", "Ticket fixe si mode manuel."],
                ["`max_concurrent_positions`", "4", "1 - 10", "Cap positions ouvertes."],
                ["`max_positions_per_tick`", "1", "1 - 3", "Débit de nouvelles positions par cycle."],
                ["`max_holding_seconds`", "180 s", "30 - 900 s", "Sortie forcée si le signal ne se matérialise pas vite."],
            ],
            [2200, 1000, 1500, CONTENT_WIDTH - 4700],
        )
    )
    body.append(
        paragraph(
            "Au runtime, `LiveHub.tick()` ajoute des garde-fous supplémentaires: quote live requise, staleness contrôlée, `complement_gap <= 0.02`, absence de position déjà ouverte sur le marché, et plancher P0 `expected_edge > 0.002`."
        )
    )

    body.append(heading("4. Risk Management", 1, page_break_before=True))
    body.append(
        paragraph(
            "Le risk management est réparti entre `RiskConfig`, `AdaptiveStrategyEngine.size_position()` et `RiskGuard`. L’objectif est d’empêcher qu’un bon signal algorithmique ne soit envoyé vers l’exécution si la taille, l’exposition globale, la dégradation API ou la perte cumulée violent les contraintes de portefeuille."
        )
    )
    body.append(
        code_block(
            """
starting_equity = equity - total_pnl
if total_pnl < 0:
    current_drawdown_pct = abs(total_pnl) / starting_equity
else:
    current_drawdown_pct = 0

HALT if current_drawdown_pct >= max_drawdown_stop_pct
HALT if consecutive_api_failures >= 3

On halt:
    set_command("stop")
    cancel_all_open_orders()
    broadcast(type="halt", reason=..., details=...)
            """.strip()
        )
    )
    body.append(
        table(
            ["Régime", "Critère volatilité", "Multiplicateur seuil", "Politique"],
            [
                ["LOW_VOL", "`volatility < 0.005`", "0.7x", "Autorise des seuils plus bas dans un marché calme."],
                ["NORMAL", "`0.005 <= vol <= 0.015`", "1.0x", "Régime nominal."],
                ["HIGH_VOL", "`0.015 < vol <= 0.03`", "1.5x", "Hausse des exigences d’entrée."],
                ["CRISIS", "`vol > 0.03`", "1.0x mais `detected=False`", "Aucune nouvelle position; signal bloqué par construction."],
            ],
            [1500, 1900, 1500, CONTENT_WIDTH - 4900],
        )
    )
    body.append(
        table(
            ["Limite", "Valeur runtime", "Action si dépassée"],
            [
                ["Perte max portefeuille", "10%", "Arrêt complet du bot + tentative d’annulation ordres."],
                ["Échecs API consécutifs", "3", "Kill switch `api_failures`."],
                ["Exposition totale", "25% de l’equity", "Aucun nouveau trade autorisé si `exposure_cap <= 0`."],
                ["Risque unitaire", "1% de l’equity", "Cap nominal avant modulation Kelly."],
                ["Positions simultanées", "4", "Blocage des nouvelles entrées au-delà."],
                ["Nouvelles positions / tick", "1", "Lissage du débit d’exécution."],
                ["Âge maximal du signal", "3 s (stale)", "Marché écarté de la sélection."],
                ["Holding max", "180 s", "Clôture automatique si âge trop élevé ou signal détérioré."],
                ["Complément YES+NO", "`gap <= 0.02`", "Marché réputé incohérent sinon."],
            ],
            [2200, 1400, CONTENT_WIDTH - 3600],
        )
    )
    body.append(
        paragraph(
            "La taille finale est volontairement prudente: même si l’edge est fort, `kelly_fraction` limite la convexité et `exposure_cap` empêche la saturation du portefeuille. En mode manuel, le runtime bascule sur un ticket fixe et n’utilise plus la formule d’allocation automatique."
        )
    )

    body.append(heading("5. Pipeline de données", 1, page_break_before=True))
    body.append(heading("5.1 WebSocket Polymarket", 2))
    body.append(
        paragraph(
            "Le canal public `wss://ws-subscriptions-clob.polymarket.com/ws/market` diffuse snapshots de carnet, updates incrémentales et événements de cycle de vie marché. Le runtime principal envoie aujourd’hui un payload de souscription conforme à la documentation officielle, par batchs de 100 assets."
        )
    )
    body.append(
        code_block(
            """
Subscription request (runtime live hub):
{
  "assets_ids": ["<token_yes>", "<token_no>", "..."],
  "type": "market"
}

Événements consommés:
- book          -> bids[] / asks[]
- price_change  -> price_changes[] avec best_bid / best_ask
- best_bid_ask  -> meilleur bid/ask compact
            """.strip()
        )
    )
    body.append(
        table(
            ["Message / champ", "Usage dans Poybot"],
            [
                ["`book.asset_id`, `market`, `bids[]`, `asks[]`", "Initialise ou réhydrate le top-of-book complet d’un token."],
                ["`price_change.price_changes[].best_bid / best_ask`", "Met à jour rapidement le meilleur bid/ask sans recharger tout le book."],
                ["`best_bid_ask.best_bid / best_ask / spread`", "Format compact compatible avec une optimisation future du parseur."],
                ["`timestamp`", "Base de fraîcheur quote et calcul de staleness côté runtime."],
            ],
            [2700, CONTENT_WIDTH - 2700],
        )
    )
    body.append(
        paragraph(
            "Note de cohérence: `PolymarketWsIngestor` persistant utilise encore un payload de type `{\"type\": \"subscribe\", \"channel\": \"market\", ...}`. Cette divergence documentaire / runtime doit être résorbée pour éviter des comportements différents entre le flux de persistance et le flux opérateur."
        )
    )

    body.append(heading("5.2 WebSocket Binance (bookTicker)", 2))
    body.append(
        paragraph(
            "Le dépôt ne contient pas encore d’ingestor Binance. Pour la stratégie de latency arbitrage, la brique cible recommandée est le stream spot officiel `<symbol>@bookTicker`, consommé en mode raw ou via souscription JSON. Cette section décrit donc l’interface cible, pas un composant déjà branché au runtime."
        )
    )
    body.append(
        code_block(
            """
Raw stream:
wss://stream.binance.com:9443/ws/btcusdt@bookTicker

Payload cible:
{
  "u": 400900217,
  "s": "BTCUSDT",
  "b": "25.35190000",
  "B": "31.21000000",
  "a": "25.36520000",
  "A": "40.66000000"
}
            """.strip()
        )
    )
    body.append(
        table(
            ["Champ", "Description", "Usage prévu"],
            [
                ["`s`", "Symbole spot", "Mapping vers les marchés Polymarket strike/expiry."],
                ["`b` / `a`", "Best bid / ask spot", "Calcul du mid spot et de la fair probability."],
                ["`B` / `A`", "Quantités bid / ask", "Filtre liquidité oracle ou signal qualité."],
                ["`u`", "Update ID", "Détection d’ordonnancement / monotonicité du flux."],
            ],
            [1200, 1900, CONTENT_WIDTH - 3100],
        )
    )

    body.append(heading("5.3 Schéma de base de données", 2))
    body.append(
        table(
            ["Table", "Colonnes clés", "Rôle"],
            [
                ["`top_of_book`", "`market_id`, `token_id`, `best_bid`, `best_ask`, `mid_price`, `spread`, `observed_at`", "Historique des meilleures limites par token."],
                ["`bot_trades`", "`id`, `market_id`, `market_title`, `outcome`, `side`, `price`, `size`, `notional`, `pnl_abs`, `pnl_pct`, `status`, `executed_at`", "Journal des exécutions bot et de leur PnL."],
                ["`portfolio_snapshots`", "`total_equity`, `capital_in_trade`, `pnl_abs`, `pnl_pct`, `observed_at`", "Séries de portefeuille utilisables pour analytics et contrôle drawdown."],
                ["`raw_websocket_messages`", "`channel`, `market_id`, `payload`, `ingested_at`", "Traçabilité brute pour replay et debug."],
                ["`trades`", "`market_id`, `token_id`, `side`, `price`, `size`, `traded_at`", "Backfill des trades CLOB bruts."],
            ],
            [1700, 3400, CONTENT_WIDTH - 5100],
        )
    )

    body.append(heading("5.4 Crons ARQ", 2))
    body.append(
        table(
            ["Job", "Fréquence", "Description"],
            [
                ["`sync_metadata_job`", "00:00 / 06:00 / 12:00 / 18:00", "Synchronise événements et marchés depuis Gamma, remplace les tokens, stocke un snapshot brut."],
                ["`refresh_recent_trades_job`", "Toutes les 15 minutes", "Backfill les trades récents sur les 20 marchés actifs les plus visibles."],
            ],
            [2200, 2200, CONTENT_WIDTH - 4400],
        )
    )
    body.append(
        paragraph(
            "Le worker ARQ dépend de Redis et de la même couche SQLAlchemy que l’API. En exploitation, ces crons doivent être complétés par des jobs dédiés au replay, aux snapshots portefeuille et au contrôle de dérive entre runtime live et stockage persistant."
        )
    )

    body.append(heading("6. Sécurité", 1, page_break_before=True))
    body.append(
        paragraph(
            "L’audit réseau du dépôt conclut à une architecture cohérente pour un MVP local mais insuffisamment durcie pour une exposition Internet. Les risques dominants sont l’absence d’auth forte par défaut, la publication de services de données, le TLS manquant et une gouvernance réseau encore permissive."
        )
    )
    body.append(
        table(
            ["Priorité", "Actions", "Justification"],
            [
                ["P0", "Ne plus publier DB/Redis/ClickHouse vers l’hôte; imposer authn/authz sur REST sensibles et WS; placer le backend derrière un reverse proxy TLS; supprimer les credentials faibles par défaut.", "Réduit immédiatement la surface d’attaque et le risque de prise de contrôle fonctionnelle."],
                ["P1", "Ajouter retry exponentiel + jitter, timeouts explicites, allow-list egress, journalisation sécurité et métriques réseau.", "Améliore la résilience aux incidents fournisseurs et l’observabilité."],
                ["P2", "Segmenter les réseaux Docker, introduire un API gateway/WAF, déplacer les secrets vers un secret manager, automatiser scans SAST/DAST minimaux.", "Durcissement production et réduction du blast radius."],
            ],
            [900, 3600, CONTENT_WIDTH - 4500],
        )
    )
    body.append(
        table(
            ["Checklist avant mise en production", "État attendu"],
            [
                ["`API_AUTH_TOKEN` et `LIVE_WS_TOKEN` non vides", "Obligatoire"],
                ["`polymarket_trading_enabled=false` tant que la chaîne live n’est pas validée", "Obligatoire"],
                ["HTTPS/WSS via reverse proxy", "Obligatoire"],
                ["Ports PostgreSQL / Redis / ClickHouse non exposés publiquement", "Obligatoire"],
                ["Secrets Polymarket stockés hors dépôt", "Obligatoire"],
                ["Logs de sécurité et rate limiting vérifiés", "Obligatoire"],
                ["Tests routes live + smoke `/health` `/ready` `/api/v1/live-summary`", "Obligatoire"],
                ["Runbook halt drawdown/API documenté", "Fortement recommandé"],
            ],
            [4200, CONTENT_WIDTH - 4200],
        )
    )
    body.append(
        paragraph(
            "Le code applique déjà un rate limiter mémoire et des tokens optionnels; toutefois la sécurité reste majoritairement “opt-in”. Le mode `live` ne doit donc pas être considéré comme prêt production sans implémentation des P0 ci-dessus."
        )
    )

    body.append(heading("7. Backtest", 1, page_break_before=True))
    body.append(
        paragraph(
            "Le script `backend/scripts/backtest.py` exécute un backtest analytique synthétique sur 2000 ticks générés via une marche aléatoire mean-reverting avec chocs de volatilité. Il pompe ces ticks dans `AdaptiveStrategyEngine`, ouvre un trade lorsqu’un signal est détecté, puis ferme sur disparition d’edge ou dépassement d’un horizon de 45 ticks."
        )
    )
    body.append(
        table(
            ["Métrique", "Description"],
            [
                ["`Trades Executed`", "Nombre total de trades simulés."],
                ["`Winning Trades` / `Win Rate`", "Trades positifs et pourcentage de réussite."],
                ["`Average Holding Time`", "Durée moyenne de détention en ticks."],
                ["`Profit Factor`", "Rapport profits bruts / pertes brutes."],
                ["`Net PnL`", "PnL total absolu du portefeuille simulé."],
            ],
            [2200, CONTENT_WIDTH - 2200],
        )
    )
    body.append(
        paragraph(
            "Limites majeures: données purement synthétiques, pas de replay d’order book réel, fermeture basée sur règles simplifiées, pas de carnet multi-niveaux, pas de coût réseau ni de queue priority, et dépendance à une seule série de marché. Le benchmark du repo signale explicitement que ce backtest ne doit pas créer de fausse confiance de production."
        )
    )

    body.append(heading("8. Guide de déploiement", 1, page_break_before=True))
    body.append(
        paragraph(
            "Le chemin recommandé reste le démarrage via Docker Compose. L’installation manuelle reste utile pour le développement local, mais la séquence ci-dessous garde le worker, Redis et le frontend alignés avec l’API."
        )
    )
    body.append(
        table(
            ["Variable", "Portée", "Obligatoire", "Commentaire"],
            [
                ["`POSTGRES_DSN`", "Backend", "Oui", "Chaîne SQLAlchemy Async vers PostgreSQL ou SQLite dev."],
                ["`REDIS_URL`", "Backend / Worker", "Oui", "Queue ARQ et scheduling."],
                ["`API_AUTH_TOKEN`", "Backend", "Recommandé", "Protège `/bot/control`, `/strategy/config`, `/execute`, `/close`."],
                ["`LIVE_WS_TOKEN`", "Backend / Frontend", "Recommandé", "Protège `/ws/live`."],
                ["`POLYMARKET_GAMMA_BASE_URL`", "Backend", "Oui", "Source métadonnées marchés."],
                ["`POLYMARKET_CLOB_REST_BASE_URL`", "Backend", "Oui", "REST quotes / trades / ordres."],
                ["`POLYMARKET_CLOB_WS_URL`", "Backend", "Oui", "Flux temps réel marché."],
                ["`POLYMARKET_TRADING_ENABLED`", "Backend", "Oui en live", "Doit rester faux hors production contrôlée."],
                ["`POLYMARKET_TRADING_MODE`", "Backend", "Oui en live", "Valeurs attendues: `dry_run` ou `live`."],
                ["`POLYMARKET_API_KEY/SECRET/PASSPHRASE`", "Backend", "Oui en live", "Credentials d’exécution réelle."],
                ["`NEXT_PUBLIC_API_BASE`", "Frontend", "Oui", "Base URL API."],
                ["`NEXT_PUBLIC_API_AUTH_TOKEN`", "Frontend", "Si API protégée", "Injecte `x-api-token` côté UI."],
                ["`NEXT_PUBLIC_LIVE_WS_TOKEN`", "Frontend", "Si WS protégé", "Ajouté au querystring du socket live."],
                ["`NEXT_PUBLIC_WALLETCONNECT_PROJECT_ID`", "Frontend", "Optionnel / recommandé", "Le fallback `demo-project-id` ne doit pas être utilisé en prod."],
            ],
            [2500, 1600, 1100, CONTENT_WIDTH - 5200],
        )
    )
    body.append(
        code_block(
            """
Séquence de démarrage (Docker):
1. cd backend
2. cp .env.example .env
3. docker compose up --build
4. docker compose run --rm api alembic upgrade head
5. Ouvrir:
   - Frontend : http://localhost:3000
   - API docs : http://localhost:8000/docs

Séquence manuelle:
1. cd backend && pip install -e .[dev]
2. alembic upgrade head
3. uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
4. cd frontend && cp .env.local.example .env.local && npm install && npm run dev
            """.strip()
        )
    )
    body.append(
        table(
            ["Healthcheck", "Résultat attendu"],
            [
                ["`GET /health`", "`{\"status\": \"ok\"}`."],
                ["`GET /ready`", "Accès DB valide, statut `ready`."],
                ["`GET /api/v1/live-summary`", "Snapshot complet avec `risk_config`, `stats`, `markets`."],
                ["`WS /ws/live`", "Réception immédiate d’un événement `bootstrap`."],
                ["Frontend dashboard", "Chargement des KPI et reconnexion WS si coupure."],
            ],
            [2400, CONTENT_WIDTH - 2400],
        )
    )
    body.append(
        paragraph(
            "Critère go-live minimal: smoke backend, tests live routes, typecheck frontend, secret management, proxy TLS et validation du mode `dry_run` avant activation du mode `live`."
        )
    )

    sect_pr = (
        "<w:sectPr>"
        '<w:footerReference w:type="default" r:id="rId3"/>'
        f'<w:pgSz w:w="{PAGE_WIDTH}" w:h="{PAGE_HEIGHT}"/>'
        f'<w:pgMar w:top="{MARGIN}" w:right="{MARGIN}" w:bottom="{MARGIN}" w:left="{MARGIN}" w:header="720" w:footer="720" w:gutter="0"/>'
        '<w:cols w:space="720"/>'
        '<w:docGrid w:linePitch="360"/>'
        "</w:sectPr>"
    )

    return xml_document(
        f"""
        <w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}">
          <w:body>
            {''.join(body)}
            {sect_pr}
          </w:body>
        </w:document>
        """.strip()
    )


def validate_xml_parts(parts: dict[str, str]) -> None:
    for path, content in parts.items():
        if not path.endswith(".xml") and not path.endswith(".rels"):
            continue
        try:
            ET.fromstring(content)
        except ET.ParseError as exc:
            raise ValueError(f"XML invalide dans {path}: {exc}") from exc


def build_docx(output_path: Path, author: str, status: str, report_date: str) -> None:
    title = "Poybot — Spécification Technique v1.0"
    parts = {
        "[Content_Types].xml": content_types(),
        "_rels/.rels": package_relationships(),
        "docProps/core.xml": core_properties(author, title),
        "docProps/app.xml": app_properties(),
        "word/document.xml": document_xml(author, status, report_date),
        "word/_rels/document.xml.rels": document_relationships(),
        "word/styles.xml": styles_xml(),
        "word/settings.xml": settings_xml(),
        "word/footer1.xml": footer_xml(),
    }
    validate_xml_parts(parts)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as archive:
        for relative_path, content in parts.items():
            archive.writestr(relative_path, content)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the Poybot technical specification DOCX.")
    parser.add_argument(
        "--output",
        default="artifacts/Poybot_Specification_Technique_v1_0.docx",
        help="Output DOCX path.",
    )
    parser.add_argument(
        "--author",
        default="Codex",
        help="Author to place on the cover page.",
    )
    parser.add_argument(
        "--status",
        default="Draft",
        choices=["Draft", "Review", "Approved"],
        help="Document status to place on the cover page.",
    )
    parser.add_argument(
        "--date",
        default=datetime.now().strftime("%d/%m/%Y"),
        help="Display date for the cover page.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_path = Path(args.output)
    build_docx(output_path=output_path, author=args.author, status=args.status, report_date=args.date)
    print(output_path.resolve())


if __name__ == "__main__":
    main()
