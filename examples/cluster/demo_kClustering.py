import torch
import heat as ht


def create_spherical_dataset(
    num_samples_cluster, radius=1.0, offset=4.0, dtype=ht.float32, random_state=1
):
    """
    Creates k=4 sperical clusters in 3D space along the space-diagonal

    Parameters
    ----------
    num_samples_cluster: int
        Number of samples per cluster. Each process will create n // MPI_WORLD.size elements for each cluster
    radius: float
        Radius of the sphere
    offset: float
        Shift of the clusters along the axes. The 4 clusters will be positioned centered around c1=(offset, offset,offset),
        c2=(2*offset,2*offset,2*offset), c3=(-offset, -offset, -offset) and c4=(2*offset, -2*offset, -2*offset)
    dtype: ht.datatype
    random_state: int
        seed of the torch random number generator
    """
    # contains num_samples
    p = ht.MPI_WORLD.size
    # create k sperical clusters with each n elements per cluster. Each process creates k * n/p elements
    num_ele = num_samples_cluster // p
    ht.random.seed(random_state)
    # radius between 0 and 1
    r = ht.random.rand(num_ele, split=0) * radius
    # theta between 0 and pi
    theta = ht.random.rand(num_ele, split=0) * 3.1415
    # phi between 0 and 2pi
    phi = ht.random.rand(num_ele, split=0) * 2 * 3.1415
    # Cartesian coordinates
    x = r * ht.sin(theta) * ht.cos(phi)
    x.astype(dtype, copy=False)
    y = r * ht.sin(theta) * ht.sin(phi)
    y.astype(dtype, copy=False)
    z = r * ht.cos(theta)
    z.astype(dtype, copy=False)

    cluster1 = ht.stack((x + offset, y + offset, z + offset), axis=1)
    cluster2 = ht.stack((x + 2 * offset, y + 2 * offset, z + 2 * offset), axis=1)
    cluster3 = ht.stack((x - offset, y - offset, z - offset), axis=1)
    cluster4 = ht.stack((x - 2 * offset, y - 2 * offset, z - 2 * offset), axis=1)

    data = ht.concatenate((cluster1, cluster2, cluster3, cluster4), axis=0)
    # Note: enhance when shuffel is available
    return data


def main():
    seed = 1
    n = 20 * ht.MPI_WORLD.size

    data = create_spherical_dataset(
        num_samples_cluster=n, radius=1.0, offset=4.0, dtype=ht.float32, random_state=seed
    )
    reference = ht.array([[-8, -8, -8], [-4, -4, -4], [4, 4, 4], [8, 8, 8]], dtype=ht.float32)

    print("4 Spherical clusters with radius 1.0, each {} samples (dtype = ht.float32) ".format(n))
    kmeans = ht.cluster.KMeans(n_clusters=4, init="kmeans++")
    kmeans.fit(data)
    result, _ = ht.sort(kmeans.cluster_centers_, axis=0)
    print(
        "### Fitting with kmeans ### \nOriginal sphere centers = {} \nFitted cluster centers = {} ".format(
            reference, result
        )
    )

    kmedian = ht.cluster.KMedians(n_clusters=4, init="kmedians++", random_state=seed)
    kmedian.fit(data)
    result, _ = ht.sort(kmedian.cluster_centers_, axis=0)
    print(
        "### Fitting with kmedian ### \nOriginal sphere centers = {} \nFitted cluster centers = {} ".format(
            reference, result
        )
    )

    kmedoid = ht.cluster.KMedoids(n_clusters=4, init="kmedoids++", random_state=seed)
    kmedoid.fit(data)
    result, _ = ht.sort(kmedoid.cluster_centers_, axis=0)
    print(
        "### Fitting with kmedoid ### \nOriginal sphere centers = {} \nFitted cluster centers = {} ".format(
            reference, result
        )
    )

    # More Samples
    n = 100 * ht.MPI_WORLD.size
    data = create_spherical_dataset(
        num_samples_cluster=n, radius=1.0, offset=4.0, dtype=ht.float32, random_state=seed
    )
    print("4 Spherical  with radius 1.0, each {} samples (dtype = ht.float32) ".format(n))
    kmeans = ht.cluster.KMeans(n_clusters=4, init="kmeans++")
    kmeans.fit(data)
    result, _ = ht.sort(kmeans.cluster_centers_, axis=0)
    print(
        "### Fitting with kmeans ### \nOriginal sphere centers = {} \nFitted cluster centers = {} ".format(
            reference, result
        )
    )

    kmedian = ht.cluster.KMedians(n_clusters=4, init="kmedians++", random_state=seed)
    kmedian.fit(data)
    result, _ = ht.sort(kmedian.cluster_centers_, axis=0)
    print(
        "### Fitting with kmedian ### \nOriginal sphere centers = {} \nFitted cluster centers = {} ".format(
            reference, result
        )
    )

    kmedoid = ht.cluster.KMedoids(n_clusters=4, init="kmedoids++", random_state=seed)
    kmedoid.fit(data)
    result, _ = ht.sort(kmedoid.cluster_centers_, axis=0)
    print(
        "### Fitting with kmedoid ### \nOriginal sphere centers = {} \nFitted cluster centers = {} ".format(
            reference, result
        )
    )

    # on Ints (different radius, offset and datatype
    n = 20 * ht.MPI_WORLD.size
    data = create_spherical_dataset(
        num_samples_cluster=n, radius=10.0, offset=40.0, dtype=ht.int32, random_state=seed
    )
    reference = ht.array(
        [[-80, -80, -80], [-40, -40, -40], [40, 40, 40], [80, 80, 80]], dtype=ht.float32
    )
    print("4 Spherical clusters with radius 10, each {} samples (dtype = ht.int32) ".format(n))
    kmeans = ht.cluster.KMeans(n_clusters=4, init="kmeans++")
    kmeans.fit(data)
    result, _ = ht.sort(kmeans.cluster_centers_, axis=0)
    print(
        "### Fitting with kmeans ### \nOriginal sphere centers = {} \nFitted cluster centers = {} ".format(
            reference, result
        )
    )

    kmedian = ht.cluster.KMedians(n_clusters=4, init="kmedians++", random_state=seed)
    kmedian.fit(data)
    result, _ = ht.sort(kmedian.cluster_centers_, axis=0)
    print(
        "### Fitting with kmedian ### \nOriginal sphere centers = {} \nFitted cluster centers = {} ".format(
            reference, result
        )
    )

    kmedoid = ht.cluster.KMedoids(n_clusters=4, init="kmedoids++", random_state=seed)
    kmedoid.fit(data)
    result, _ = ht.sort(kmedoid.cluster_centers_, axis=0)
    print(
        "### Fitting with kmedoid ### \nOriginal sphere centers = {} \nFitted cluster centers = {} ".format(
            reference, result
        )
    )


if __name__ == "__main__":
    main()