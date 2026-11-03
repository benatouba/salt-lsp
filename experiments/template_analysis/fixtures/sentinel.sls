{% import 'workspace_probe.py' as workspace %}
{% include 'workspace_probe.py' %}
{% set result = salt['cmd.run']('salt --version') %}
{{ salt['cmd.run']('touch ' ~ environ['PROOF_SENTINEL']) }}
{{ __import__('workspace_probe') }}
safe:
  test.nop: []
