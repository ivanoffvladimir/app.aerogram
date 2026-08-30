LOGISTICS OS — FINAL TZ v3

CORE DOCUMENTS
01_FINAL_Product_TZ.docx — what and why to build.
02_FINAL_Logistics_OS_System_TZ.docx — logistics/system processes and carrier layer.
03_FINAL_Developer_Frontend_TZ.docx — implementation requirements for Frontend.
04_FINAL_Developer_Backend_TZ.docx — implementation requirements for Backend.

TECHNICAL CONTRACT
05_FINAL_OpenAPI_3_1.yaml
06_FINAL_ERD.png
07_JSON_Examples/
08_Competitive_Matrix.xlsx
09_Carrier_Adapter_Matrix.csv

START ORDER
1. Freeze P0 OpenAPI and schemas.
2. Create repos/environments/CI and mock server.
3. Frontend starts against mocks.
4. Backend implements core + Major Express + one REST adapter.
5. Add remaining adapters and run pilot E2E.

OPEN ITEMS THAT DO NOT BLOCK DEVELOPMENT
- Exact production credentials and contract-specific fields for each pilot carrier.
- Final commercial SaaS pricing.
- Exact tenant-specific insurance thresholds/routing policy values.
- ML model: intentionally deferred until sufficient observed data.
