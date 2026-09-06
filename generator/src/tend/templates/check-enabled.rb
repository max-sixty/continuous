# Inspect the parsed YAML node so the extra YAML 1.1 boolean words (yes/no/on/off)
# do not diverge from the YAML 1.2 parser used by `tend init`.
require "psych"

path = ARGV.fetch(0)
documents = Psych.parse_stream(File.read(path, mode: "r:bom|utf-8")).children
unless documents.length == 1
  abort "tend config must contain exactly one YAML document"
end

mapping = documents.first.root
unless mapping.is_a?(Psych::Nodes::Mapping)
  abort "tend config must contain a YAML mapping"
end

def has_yaml_merge_key?(node)
  case node
  when Psych::Nodes::Mapping
    node.children.each_slice(2).any? do |key, value|
      (key.is_a?(Psych::Nodes::Scalar) && key.plain && key.value == "<<") ||
        has_yaml_merge_key?(key) || has_yaml_merge_key?(value)
    end
  when Psych::Nodes::Sequence
    node.children.any? { |value| has_yaml_merge_key?(value) }
  else
    false
  end
end

if has_yaml_merge_key?(mapping)
  abort "tend config: YAML merge keys (<<) are not supported"
end

matches = mapping.children.each_slice(2).select do |key, _value|
  key.is_a?(Psych::Nodes::Scalar) && key.value == "enabled"
end
abort "tend config: enabled must appear at most once" if matches.length > 1

value = matches.dig(0, 1)
enabled = true
if value
  bool_tag = value.respond_to?(:tag) && value.tag == "tag:yaml.org,2002:bool"
  literal = value.value.downcase if value.is_a?(Psych::Nodes::Scalar)
  unless value.is_a?(Psych::Nodes::Scalar) &&
      (value.plain || bool_tag) &&
      ["true", "false"].include?(literal)
    abort "tend config: enabled must be true or false"
  end
  enabled = literal == "true"
end

puts "enabled=#{enabled}"

unless enabled
  warn "::notice title=Tend disabled::The tend config sets enabled: false; skipping this job"
end
