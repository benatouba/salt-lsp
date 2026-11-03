{% import 'map.jinja' as map %}
{% from "lib.jinja" import helper %}
{% include 'fragment.jinja' %}
include:
  - common
{% if grains['os'] == 'Debian' %}
conditional:
  pkg.installed:
    - name: {{ pillar['package'] }}
    - context:
        nested: [one, "{{ value }}"]
{% endif %}
{% for service in services %}
{{ service }}:
  service.running:
    - enable: true
{% endfor %}
after:
  test.nop: []
