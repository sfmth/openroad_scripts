import openroad
from openroad import Design, Tech, Timing
import rcx
import os
import odb


openroad.openroad_version()

odb_file = "results/nangate45/ariane133/base/2_2_floorplan_macro.odb"
lef_files = [#"platforms/nangate45/lef/NangateOpenCellLibrary.macro.lef",
#                "platforms/nangate45/lef/NangateOpenCellLibrary.macro.mod.lef",
 #               "platforms/nangate45/lef/NangateOpenCellLibrary.macro.rect.lef",
                "platforms/nangate45/lef/NangateOpenCellLibrary.tech.lef"]
lib_files = ["platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib"]

tech = Tech()
for lef_file in lef_files:
    tech.readLef(lef_file)
for lib_file in lib_files:
    tech.readLiberty(lib_file)

design = Design(tech)

design.readDb(odb_file)


# Clustering parameters (in database units)
X_TOLERANCE = 10000  # 10um in database units (assuming 1000 DBU per micron)
Y_TOLERANCE = 1000  # 1um in database units


def distance_within_tolerance(pos1, pos2, x_tol, y_tol):
    """Check if two positions are within the specified tolerances"""
    x_diff = abs(pos1[0] - pos2[0])
    y_diff = abs(pos1[1] - pos2[1])
    return x_diff <= x_tol and y_diff <= y_tol


# Gather all instances with their positions
instances = []
for inst in design.getBlock().getInsts():
    name = inst.getName()
    if (
        "FILLER" in name
        or "TAP" in name
        or "decap" in inst.getMaster().getName()
        or not inst.isPlaced()
    ):
        continue

    x, y = inst.getLocation()
    instances.append({"name": name, "x": x, "y": y, "clustered": False})

print(f"Found {len(instances)} instances to cluster")

# Clustering algorithm
clusters = []
cluster_id = 0

for i, inst in enumerate(instances):
    if inst["clustered"]:
        continue

    # Start a new cluster
    cluster = [inst["name"]]
    inst["clustered"] = True

    # Find all instances within tolerance of this one
    for j, other_inst in enumerate(instances):
        if i == j or other_inst["clustered"]:
            continue

        if distance_within_tolerance(
            (inst["x"], inst["y"]),
            (other_inst["x"], other_inst["y"]),
            X_TOLERANCE,
            Y_TOLERANCE,
        ):
            cluster.append(other_inst["name"])
            other_inst["clustered"] = True

    clusters.append(cluster)
    cluster_id += 1

print(f"Created {len(clusters)} clusters")


def reduce_cluster_names(clusters):
    """
    Iteratively reduce instance names by finding smallest unique prefixes and removing
    instances that share the same prefix. Uses hierarchical delimiter '/'.
    Prefixes must be unique to the cluster - they cannot match any string in other clusters.
    """
    reduced_clusters = []
    total_iterations = 0
    total_prefixes_found = 0
    total_instances_matched = 0
    rejected_prefixes = set()  # Keep track of rejected prefixes to avoid retrying
    
    print("\n=== CLUSTER REDUCTION STATISTICS ===")
    print(f"Starting reduction process with {len(clusters)} clusters")
    
    # Sort clusters by size (largest first)
    sorted_clusters = sorted(clusters, key=len, reverse=True)
    
    # Create a flat list of all instance names across all clusters for uniqueness checking
    all_instances = []
    for cluster in sorted_clusters:
        all_instances.extend(cluster)
    
    # Print initial cluster size statistics
    initial_sizes = [len(cluster) for cluster in sorted_clusters]
    print(f"\nInitial cluster sizes:")
    print(f"  Total instances: {sum(initial_sizes)}")
    print(f"  Min size: {min(initial_sizes)}")
    print(f"  Max size: {max(initial_sizes)}")
    print(f"  Average size: {sum(initial_sizes) / len(initial_sizes):.1f}")
    print(f"  Single instance clusters: {sum(1 for size in initial_sizes if size == 1)}")
    
    for cluster_idx, cluster in enumerate(sorted_clusters):
        if len(cluster) == 1:
            # Single instance cluster - use full name
            reduced_clusters.append(cluster)
            continue
            
        print(f"\n--- Processing Cluster {cluster_idx} (size: {len(cluster)}) ---")
        
        # Sort instance names by length (largest first) within the cluster
        sorted_cluster = sorted(cluster, key=len, reverse=True)
        
        # Work with a copy of the cluster that we'll modify
        remaining_instances = sorted_cluster.copy()
        final_representatives = []
        cluster_iterations = 0
        
        # Keep iterating until no instances remain
        while remaining_instances:
            cluster_iterations += 1
            total_iterations += 1
            
            # Pick the first remaining instance that doesn't have a wildcard
            current_instance = None
            for instance in remaining_instances:
                if '*' not in instance:
                    current_instance = instance
                    break
            
            # If no instance without wildcard found, we're done with this cluster
            if current_instance is None:
                break
            parts = current_instance.split('/')
            
            # Find the smallest prefix that uniquely identifies this instance
            found_unique_prefix = None
            
            # Generate prefix lengths: increase until next forward slash, open square bracket, or digit
            prefix_lengths = []
            
            # Find positions of forward slashes, open square brackets, dots, and digits
            delimiter_positions = []
            for i, char in enumerate(current_instance):
                if char == '/' or char == '[' or char == '.' or char.isdigit():
                    delimiter_positions.append(i)
            
            if delimiter_positions:
                # Start with first delimiter position + 1 (include the delimiter)
                for pos in delimiter_positions:
                    prefix_length = pos + 1
                    # Only add if it's not the entire string
                    if prefix_length < len(current_instance):
                        prefix_lengths.append(prefix_length)
            else:
                # No delimiters found, skip this instance (prefix would be the whole string)
                prefix_lengths = []
            
            # If no prefix lengths generated (no delimiters), skip this instance
            if not prefix_lengths:
                final_representatives.append(current_instance)
                remaining_instances.remove(current_instance)
                continue
            
            for prefix_len in prefix_lengths:
                candidate_prefix = current_instance[:prefix_len]
                
                # Skip if this prefix was already rejected
                if candidate_prefix in rejected_prefixes:
                    continue
                
                # Skip if prefix equals the whole instance (shouldn't happen but safety check)
                if candidate_prefix == current_instance:
                    continue
                
                print(f"Trying prefix '{candidate_prefix}' (from instance '{current_instance}')")
                
                # Check if this prefix matches instances in the current cluster
                matching_instances_in_cluster = []
                for other_name in remaining_instances:
                    if other_name.startswith(candidate_prefix):
                        matching_instances_in_cluster.append(other_name)

                # Only check other clusters if there's more than one match in current cluster
                matches_other_clusters = False
                first_other_match = None
                if len(matching_instances_in_cluster) > 1:
                    # Check if this prefix matches any instances in OTHER clusters (stop at first match)
                    for other_name in all_instances:
                        if other_name not in remaining_instances and other_name.startswith(candidate_prefix):
                            matches_other_clusters = True
                            first_other_match = other_name
                            break  # Stop at first match

                if matches_other_clusters:
                    rejected_prefixes.add(candidate_prefix)  # Add to rejected prefixes map

                # Prefix is valid if it matches at least TWO instances in current cluster AND doesn't match any in other clusters
                if len(matching_instances_in_cluster) >= 2 and not matches_other_clusters:
                    found_unique_prefix = candidate_prefix
                    print(f"Accepted prefix '{candidate_prefix}' (from instance '{current_instance}') matching {len(matching_instances_in_cluster)} instances")
                    break
            
            # Use the cluster-unique prefix (or full name if no cluster-unique prefix found)
            if found_unique_prefix is None:
                found_unique_prefix = current_instance
            
            # Remove all instances that start with this prefix (only from current cluster)
            instances_to_remove = []
            for instance in remaining_instances:
                if instance.startswith(found_unique_prefix):
                    instances_to_remove.append(instance)
            
            instances_matched = len(instances_to_remove)
            total_prefixes_found += 1
            total_instances_matched += instances_matched
            

            
            if instances_matched > 1:
                print(f"  Iteration {cluster_iterations}: Found unique prefix '{found_unique_prefix}' (from instance '{current_instance}') matching {instances_matched} instances")
                # Add the prefix with wildcard to replace the removed instances
                final_representatives.append(found_unique_prefix + "*")
                # Remove all instances that match this prefix
                for instance in instances_to_remove:
                    remaining_instances.remove(instance)
            else:
                # Only one instance matches, keep the full instance name and just remove it
                final_representatives.append(current_instance)
                remaining_instances.remove(current_instance)
            
            # Print statistics every 1000 iterations
            if total_iterations % 1000 == 0:
                current_sizes = []
                for i, rc in enumerate(reduced_clusters):
                    current_sizes.append(len(rc))
                # Add current cluster being processed
                if final_representatives:
                    current_sizes.append(len(final_representatives))
                # Add remaining clusters not yet processed
                for j in range(cluster_idx + 1, len(sorted_clusters)):
                    current_sizes.append(len(sorted_clusters[j]))
                
                print(f"\n*** PROGRESS UPDATE (after {total_iterations} iterations) ***")
                print(f"  Prefixes found so far: {total_prefixes_found}")
                print(f"  Total instances matched: {total_instances_matched}")
                print(f"  Current cluster sizes:")
                print(f"    Total clusters: {len(current_sizes)}")
                print(f"    Min size: {min(current_sizes) if current_sizes else 0}")
                print(f"    Max size: {max(current_sizes) if current_sizes else 0}")
                print(f"    Average size: {sum(current_sizes) / len(current_sizes):.1f}" if current_sizes else 0)
                print(f"    Single instance clusters: {sum(1 for size in current_sizes if size == 1)}")
        
        print(f"  Cluster {cluster_idx} reduced from {len(cluster)} to {len(final_representatives)} representatives")
        reduced_clusters.append(final_representatives)
    
    # Print final statistics
    final_sizes = [len(cluster) for cluster in reduced_clusters]
    print(f"\n=== FINAL REDUCTION STATISTICS ===")
    print(f"Total iterations: {total_iterations}")
    print(f"Total unique prefixes found: {total_prefixes_found}")
    print(f"Total instances matched by prefixes: {total_instances_matched}")
    print(f"Total rejected prefixes: {len(rejected_prefixes)}")
    print(f"\nFinal cluster sizes:")
    print(f"  Total instances: {sum(final_sizes)}")
    print(f"  Min size: {min(final_sizes)}")
    print(f"  Max size: {max(final_sizes)}")
    print(f"  Average size: {sum(final_sizes) / len(final_sizes):.1f}")
    print(f"  Single instance clusters: {sum(1 for size in final_sizes if size == 1)}")
    
    print(f"\nReduction summary:")
    print(f"  Before: {sum(initial_sizes)} total instances in {len(sorted_clusters)} clusters")
    print(f"  After: {sum(final_sizes)} total representatives in {len(reduced_clusters)} clusters")
    print(f"  Reduction ratio: {sum(final_sizes) / sum(initial_sizes):.3f}")
    
    return reduced_clusters


# Save original clusters to file
original_output_file = "original_instance_clusters.txt"
with open(original_output_file, "w") as f:
    for i, cluster in enumerate(clusters):
        f.write(f"# Cluster {i}\n")
        for instance_name in sorted(cluster):
            f.write(f"{instance_name}\n")
        f.write("\n")

print(f"Original clustering results written to {original_output_file}")

# Reduce instance names
print("Reducing instance names...")
reduced_clusters = reduce_cluster_names(clusters)

# Save reduced clusters to file
reduced_output_file = "reduced_instance_clusters.txt"
with open(reduced_output_file, "w") as f:
    for i, cluster in enumerate(reduced_clusters):
        f.write(f"# Cluster {i}\n")
        for instance_name in sorted(cluster):
            f.write(f"{instance_name}\n")
        f.write("\n")

print(f"Reduced clustering results written to {reduced_output_file}")

# Print summary statistics
cluster_sizes = [len(cluster) for cluster in clusters]
print(f"Cluster size statistics:")
print(f"  Min size: {min(cluster_sizes)}")
print(f"  Max size: {max(cluster_sizes)}")
print(f"  Average size: {sum(cluster_sizes) / len(cluster_sizes):.1f}")
print(f"  Single instance clusters: {sum(1 for size in cluster_sizes if size == 1)}")

