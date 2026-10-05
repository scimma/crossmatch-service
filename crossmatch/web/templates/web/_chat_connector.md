{% autoescape off %}{% with chat=ref.chat_connector %}## {{ chat.heading }}

{{ chat.intro }}

- [Connector URL]({{ chat.url }}): {{ chat.url }} (MCP over HTTP POST, no authentication). Tools: {% for tool in chat.tools %}`{{ tool.name }}`{% if not forloop.last %}, {% endif %}{% endfor %}.
- {{ chat.requirement }}{% for app in chat.apps %}
- {{ app.name }}:{% for step in app.steps %} {{ step }}{% endfor %}{% endfor %}
- {{ chat.note }}
{% endwith %}{% endautoescape %}