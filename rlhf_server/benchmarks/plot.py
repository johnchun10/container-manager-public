import json
import matplotlib.pyplot as plt

# Read data from JSONL file
data = []
with open('fork_performance.jsonl', 'r') as f:
    for line in f:
        line = line.strip()
        if line:
            data.append(json.loads(line))

# Extract data for plotting
total_containers = [d['total_containers'] for d in data]
creation_rates = [d['start_rate_containers_per_sec'] for d in data]
deletion_rates = [d['stop_rate_containers_per_sec'] for d in data]

# Create plot
plt.figure(figsize=(5, 3))
plt.plot(total_containers, creation_rates, marker='o', label='Creation Rate')
plt.plot(total_containers, deletion_rates, marker='s', label='Deletion Rate')
plt.xlabel('Total Containers')
plt.ylabel('Rate (containers/sec)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig('performance_plot.pdf', bbox_inches='tight')
