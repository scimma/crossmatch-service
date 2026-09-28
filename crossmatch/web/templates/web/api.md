{% autoescape off %}# {{ ref.title }} API

Read-only JSON queries over the Rubin alert objects this service has ingested and their positional crossmatches. Service version {{ ref.service_version }}; contract version {{ ref.contract_version }}. Every configured fact on this page is read from the live configuration of the running service.

Machine-readable: [OpenAPI 3.1 document]({{ ref.urls.openapi }}), [llms.txt]({{ ref.urls.llms }}). For people: [HTML reference]({{ ref.urls.html }}).

## Conventions

- {{ ref.conventions }}
- {{ ref.nearest_rule }}
- {{ ref.caveats.coincidence }}
- {{ ref.caveats.not_in_service }}
- {{ ref.caveats.paging }}
- Every successful response of the new queries carries a `provenance` block: `service_version`, `contract_version`, `crossmatch_radius_arcsec`, `catalogs` (name and release), and `reliability_cuts` (per broker). Together they are the methods sentence for a paper.
- `diaObjectId` is a 64-bit integer; read `diaObjectId_str` in JavaScript, which loses precision above 2^53.

## Crossmatch configuration

- Crossmatch radius: {% if ref.radius_arcsec is not None %}{{ ref.radius_arcsec }} arcsec{% else %}not configured{% endif %}
- TNS search radius: default {{ ref.tns.default_radius_arcsec }} arcsec, at most {{ ref.tns.max_radius_arcsec }} arcsec
- Provenance recorded per object from service release {{ ref.provenance_recording_release }}
- Reliability cuts (minimum LSST reliability per broker; a broker-enforced value is declared by the maintainer, not observed by this service):{% for cut in ref.reliability_cuts %}
  - `{{ cut.broker }}`: {% if cut.min_reliability is not None %}{{ cut.min_reliability }}{% else %}not declared{% endif %}; enforced by `{{ cut.enforced_by }}`; status `{{ cut.status }}`{% if cut.as_of %}; as of {{ cut.as_of }}{% endif %}{% endfor %}

## Catalogs
{% for cat in ref.catalogs %}
### `{{ cat.name }}` ({{ cat.release }})

- `catalog_source_id` (column `{{ cat.catalog_source_id.column }}`): {{ cat.catalog_source_id.description }}
- Coverage map: {{ cat.coverage_map.note }}
- Filterable fields:{% for field in cat.filterable_fields %}
  - `{{ field.name }}` ({{ field.unit }}): {{ field.description }}. Parameters: {% for p in field.parameters %}`{{ p }}`{% if not forloop.last %}, {% endif %}{% endfor %}{% empty %} none{% endfor %}
{% empty %}
No catalogs are configured.
{% endfor %}
## Limits

- Request budget: {{ ref.limits.request_budget_seconds }} s (over it: 400 `query_too_expensive`)
- IDs per request: {{ ref.limits.max_ids }}
- Positions and TNS names per request: {{ ref.limits.max_positions }}
- Search radius: at most {{ ref.limits.max_cone_radius_arcsec }} arcsec
- Objects listed per position: {{ ref.limits.max_objects_per_position }}; per request: {{ ref.limits.max_objects_per_request }}
- recent-crossmatches: default window {{ ref.recent.default_window_hours }} h, at most {{ ref.recent.max_window_hours }} h; page size default {{ ref.recent.default_page_size }}, at most {{ ref.recent.max_page_size }}

## Detail levels

Cumulative; the default is `{{ ref.default_detail }}`.
{% for level in ref.detail_levels %}
- `{{ level.name }}`: {{ level.description }}{% endfor %}

## Response modes
{% for mode in ref.response_modes %}
- `{{ mode.name }}`: {{ mode.description }}{% endfor %}

## Generic filters

Catalog property filters are listed per catalog above.
{% for f in ref.generic_filters %}
- `{{ f.name }}`{% if f.unit %} ({{ f.unit }}){% endif %}{% if f.is_match_filter %}, match filter{% else %}, object filter{% endif %}: {{ f.description }}{% endfor %}

## Operations
{% for op in ref.operations %}
### `{{ op.operation_id }}`

`{{ op.method }} {{ op.path }}`

{{ op.summary }}

{{ op.description }}
{% if op.parameters %}
Parameters:
{% for p in op.parameters %}
- `{{ p.name }}` ({{ p.location }}, {% if p.required %}required{% else %}optional{% endif %}{% if p.unit %}, {{ p.unit }}{% endif %}): {{ p.description }}{% endfor %}
{% endif %}{% if op.request_body %}
Request body: `{{ op.request_body }}` (JSON; see the OpenAPI document).
{% endif %}
Responses:
{% for r in op.responses %}
- `{{ r.status }}`: {{ r.description }}{% endfor %}
{% endfor %}
## Codes

Codes are fixed snake_case values; branch on them, not on messages.
{% for group in ref.code_groups %}
### {{ group.heading }} (`{{ group.component }}`)
{% for c in group.codes %}
- `{{ c.code }}`: {{ c.description }}{% endfor %}
{% endfor %}{% endautoescape %}