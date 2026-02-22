# REGULATORY.md

| Field | Value |
|---|---|
| **Document** | REGULATORY.md — Regulatory Landscape & Compliance Standards |
| **Version** | 1.0.0 |
| **Parent** | TEAMS.md v1.3.0 |
| **Last Modified** | 2025-02-21 |

---

## How This Document Is Used

When TEAMS.md is loaded alongside this file, the following roles reference it directly:

- **Legal Adversary** — checks whether the design could be interpreted as negligence under applicable regulations.
- **Skeptical Auditor** — checks whether evidence and audit trails meet the standards listed here.
- **The Policeman** — checks whether enforcement matches regulatory requirements, not just internal policy.
- **Elephant** — checks whether the audit trail will satisfy regulators in 5 years.
- **The Archivist** — checks whether records meet retention and readability requirements.
- **Tortoise** — checks whether hardening meets the standard under sustained scrutiny.
- **The Database Engineer** — checks whether data handling meets integrity and sovereignty requirements.
- **The Librarian** — checks whether documentation matches behavior, because regulators will check.

**If a regulatory regime is marked as "Applies" below, every review must include at least one finding that addresses compliance with that regime.** Silence on an applicable regulation is a finding in itself.

---

## 1. Active Regulatory Regimes

*(Mark each as Applies / Does Not Apply / Under Evaluation. If "Applies," all relevant roles must address it.)*

### 1.1 Data Protection & Privacy

| Standard | Status | Notes |
|---|---|---|
| **GDPR** (EU General Data Protection Regulation) | Applies | Data processing, consent, right to deletion, breach notification (72h), DPO requirement, data processing agreements. Fines up to 4% of annual global turnover. |
| **BDSG** (Bundesdatenschutzgesetz — German Federal Data Protection Act) | Applies | German implementation of GDPR with additional provisions. Employee data protection, video surveillance rules. |
| **ePrivacy Directive** (EU) | Applies | Cookie consent, electronic communications privacy. Pending ePrivacy Regulation replacement. |

### 1.2 Information Security

| Standard | Status | Notes |
|---|---|---|
| **ISO 27001** | Applies | Information security management system. Certification requires annual audits. |
| **BSI IT-Grundschutz** | Applies | German Federal Office for Information Security baseline protection. More prescriptive than ISO 27001. |
| **NIS2 Directive** (EU) | Applies | Network and information security. Expanded scope, stricter penalties. Applies from October 2024. Incident reporting within 24h. |
| **SOC 2 (Type I & II)** | Applies | Service organization controls. Often required by enterprise clients. Type II requires sustained compliance over time. |
| **C5** (Cloud Computing Compliance Criteria Catalogue) | Applies | BSI's cloud security standard. Required for German public sector cloud. |
| **Common Criteria (ISO 15408)** | Applies | International security evaluation standard. |

### 1.3 Accessibility

| Standard | Status | Notes |
|---|---|---|
| **European Accessibility Act (EAA)** | Applies | EU directive requiring digital products and services to meet accessibility standards. Enforcement from June 2025. |
| **BFSG** (Barrierefreiheitsstärkungsgesetz) | Applies | German implementation of EAA. Applies to products and services placed on the market after June 28, 2025. |
| **WCAG 2.2** (Level AA) | Applies | Web Content Accessibility Guidelines. De facto technical standard for EAA/BFSG compliance. |
| **BITV 2.0** | Applies | German federal accessibility regulation for public sector websites. Based on WCAG. |
| **EN 301 549** | Applies | European standard for ICT accessibility. Referenced by EAA. |

### 1.4 AI & Algorithmic Systems

| Standard | Status | Notes |
|---|---|---|
| **EU AI Act** | Applies | Risk-based regulation of AI systems. High-risk categories require conformity assessments, documentation, human oversight. Phased enforcement 2024–2027. |
| **ISO 42001** | Applies | AI management system standard. Framework for responsible AI development. |

### 1.5 Financial Services

*(Skip this section if not applicable.)*

| Standard | Status | Notes |
|---|---|---|
| **PCI-DSS** | Applies | Payment card data security. Mandatory if touching card numbers. |
| **PSD2 / PSD3** | Applies | Payment services directive. Strong customer authentication, open banking. |
| **DORA** (Digital Operational Resilience Act) | Applies | ICT risk management for financial entities. Applies from January 2025. |
| **BaFin** regulations | Applies | German financial supervisory authority requirements. |
| **MiFID II** | Applies | EU markets regulation. Transaction reporting, transparency. |
| **SOX** (Sarbanes-Oxley) | Applies | US-listed companies. Audit trails, internal controls. |

### 1.6 Healthcare

*(Skip this section if not applicable.)*

| Standard | Status | Notes |
|---|---|---|
| **MDR** (EU Medical Device Regulation) | Applies | If software qualifies as a medical device (Software as Medical Device — SaMD). |
| **IEC 62304** | Applies | Software lifecycle for medical devices. |
| **DiGA** (Digitale Gesundheitsanwendungen) | Applies | German Digital Health Applications regulation. Requires BfArM listing. |
| **gematik** standards | Applies | German national agency for digital health infrastructure. Telematics infrastructure. |
| **HIPAA** | Applies | US only. Relevant if US users/data are in scope. |
| **HL7 FHIR** | Applies | Healthcare data interoperability standard. |

### 1.7 Automotive / Industrial

*(Skip this section if not applicable.)*

| Standard | Status | Notes |
|---|---|---|
| **ISO 26262** | Applies | Functional safety for automotive. |
| **IEC 62443** | Applies | Industrial cybersecurity. |
| **UNECE R155/R156** | Applies | Vehicle cybersecurity and software update regulations. |

### 1.8 Government / Public Sector

*(Skip this section if not applicable.)*

| Standard | Status | Notes |
|---|---|---|
| **BSI IT-Grundschutz** | Applies | Baseline security framework. |
| **C5** | Applies | Cloud security for public sector. |
| **FedRAMP** | Applies | US government cloud. Only if selling to US gov. |

---

## 2. Regulatory Bodies

Reference list of authorities that may audit, certify, or enforce.

| Body | Jurisdiction | Scope |
|---|---|---|
| **BSI** (Bundesamt für Sicherheit in der Informationstechnik) | Germany | IT security, certification, IT-Grundschutz, C5 |
| **BfDI** (Bundesbeauftragter für den Datenschutz) | Germany | Federal data protection supervision |
| **LfDI Sachsen** | Saxony | State-level data protection authority (your jurisdiction) |
| **BaFin** | Germany | Financial supervision |
| **BfArM** | Germany | Medical device and DiGA approval |
| **gematik** | Germany | Digital health infrastructure |
| **ENISA** | EU | Cybersecurity agency, NIS2 coordination |
| **European Commission** | EU | GDPR enforcement coordination, AI Act |

---

## 3. Compliance Checklist Integration

For every Tier 1 review, the output template should include:

### Regulatory Compliance Check

For each regime marked "Applies":
- **Regime**: *(name)*
- **Relevant requirement**: *(specific clause or obligation)*
- **Current status**: *(compliant / non-compliant / not yet assessed)*
- **Gap**: *(what is missing)*
- **Risk if unaddressed**: *(fine, audit failure, market access blocked)*

---

## 4. Data Sovereignty & Residency

| Question | Answer |
|---|---|
| Where must data be stored? | *(EU / Germany / specific provider)* |
| Where must data be processed? | |
| Are there restrictions on sub-processors? | |
| Is cross-border transfer required? | |
| If yes, under what mechanism? (SCCs, adequacy decision) | |

---

## 5. Retention & Deletion

| Data Category | Retention Period | Legal Basis | Deletion Process |
|---|---|---|---|
| *(e.g., user accounts)* | | | |
| *(e.g., transaction logs)* | | | |
| *(e.g., audit trails)* | | | |

---

*This document is a living checklist. It should be reviewed whenever the project enters a new market, adds a new data category, or a regulatory deadline approaches.*