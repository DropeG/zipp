## ADDED Requirements

### Requirement: Paid Mercado Libre orders create stock tasks
The system SHALL create one idempotent Shopify stock task for each processable line item in a paid Mercado Libre order.

#### Scenario: Paid order with one SKU line is processed
- **WHEN** a paid Mercado Libre order contains one line item with SKU `SOP-BAS-58` and quantity `1`
- **THEN** the system creates one `source = 'meli'` stock task for that order line

#### Scenario: Repeated notification does not duplicate work
- **WHEN** the same Mercado Libre order notification is processed more than once
- **THEN** the system keeps only one stock task per Mercado Libre order line and does not schedule multiple decrements

#### Scenario: Non-paid order is ignored safely
- **WHEN** a Mercado Libre order has a status other than `paid`
- **THEN** the system does not create an automatically applicable Shopify stock task and records the reason

### Requirement: Mercado Libre order lines resolve SKUs before stock changes
The system SHALL resolve a SKU for each Mercado Libre order line before creating an automatically applicable Shopify stock task.

#### Scenario: SKU exists directly on the order item
- **WHEN** a Mercado Libre order line includes a SKU on the order item payload
- **THEN** the system uses that SKU as the Shopify matching key

#### Scenario: SKU requires item fallback
- **WHEN** a Mercado Libre order line does not include a direct SKU but includes an item id
- **THEN** the system fetches the Mercado Libre item or variation and uses its seller SKU when available

#### Scenario: SKU cannot be resolved
- **WHEN** a Mercado Libre order line has no resolvable SKU
- **THEN** the system marks the task or order line as `needs_review` and MUST NOT change Shopify stock

### Requirement: Shopify variant matching is exact and unambiguous
The system SHALL match Mercado Libre sale lines to Shopify variants by exact SKU and SHALL apply stock changes only when exactly one Shopify variant matches.

#### Scenario: Exactly one Shopify variant matches
- **WHEN** a Mercado Libre sale line has a SKU that exists on exactly one Shopify variant
- **THEN** the system stores that Shopify variant id and can mark the task `ready_to_apply`

#### Scenario: No Shopify variant matches
- **WHEN** no Shopify variant has the Mercado Libre sale line SKU
- **THEN** the system marks the task `needs_review` or `skipped_not_in_shopify` and MUST NOT change Shopify stock

#### Scenario: Multiple Shopify variants match
- **WHEN** more than one Shopify variant has the same Mercado Libre sale line SKU
- **THEN** the system marks the task `needs_review` and MUST NOT change Shopify stock

### Requirement: Dry-run reports intended Shopify stock decrements
The system SHALL support a dry-run mode that fetches current Shopify stock and reports the intended target stock without modifying Shopify.

#### Scenario: Dry-run for a valid paid sale
- **WHEN** dry-run processes a paid Mercado Libre sale for quantity `1` and current Shopify stock is `2`
- **THEN** it reports target Shopify stock `1` and leaves Shopify unchanged

#### Scenario: Dry-run target cannot go below zero
- **WHEN** dry-run processes a valid sale where quantity sold is greater than current Shopify stock
- **THEN** it reports target Shopify stock `0` and records that the sale exceeded available Shopify stock

### Requirement: Apply decrements and confirms Shopify inventory
The system SHALL support an apply mode that decrements Shopify inventory for ready Mercado Libre sale tasks and confirms the resulting stock.

#### Scenario: Apply valid stock task
- **WHEN** apply processes a ready Mercado Libre sale task for quantity `1` and fresh Shopify stock is `2`
- **THEN** it sets Shopify stock to `1`, confirms Shopify reports `1`, and marks the task `synced`

#### Scenario: Apply re-reads current stock
- **WHEN** a ready task is applied after Shopify stock changed since dry-run
- **THEN** the system computes the target stock from the fresh Shopify stock rather than a stale dry-run value

#### Scenario: Shopify confirmation differs
- **WHEN** Shopify accepts an inventory update but the confirmed stock does not equal the intended target
- **THEN** the system marks the task `needs_review` and logs the expected and confirmed values

### Requirement: Processing is auditable
The system SHALL record task status transitions and relevant Mercado Libre/Shopify context in local logs.

#### Scenario: Successful sync is logged
- **WHEN** a Mercado Libre sale task is applied and confirmed
- **THEN** the system logs the Mercado Libre order id, SKU, quantity sold, Shopify variant id, stock before, stock after, and final `synced` status

#### Scenario: Reviewable condition is logged
- **WHEN** processing stops because data is missing, ambiguous, or unsafe
- **THEN** the system logs the reason and enough context for manual review
