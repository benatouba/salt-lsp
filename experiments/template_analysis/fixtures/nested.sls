{% if enabled %}
{% for name in names %}
nested:
  file.managed:
    - context:
        items:
          - key: "{{ name }}"
    - contents: |
        A nested-looking YAML key:
          is_not.a_function: true
{% endfor %}
{% endif %}
include:
  - *unknown_alias
outside:
  test.nop:
    - name: !!python/object/apply:os.system ['salt --version']
