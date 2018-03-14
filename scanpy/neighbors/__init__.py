import numpy as np
import scipy as sp
from scipy.sparse import issparse
from joblib import Parallel, delayed
from textwrap import dedent
from .. import settings
from .. import logging as logg
from .. import utils
from ..tools._utils import choose_representation, doc_use_rep

N_DCS = 15  # default number of diffusion components
N_PCS = 50  # default number of PCs


def neighbors(
        adata,
        use_rep=None,
        n_neighbors=30,
        knn=True,
        umap=False,
        weights={'distances', 'similarities'},
        n_jobs=None,
        copy=False):
    __doc__ = dedent("""\
    Compute a neighborhood graph of observations.

    Parameters
    ----------
    adata : :class:`~scanpy.api.AnnData`
        Annotated data matrix.
    {use_rep}
    n_neighbors : `int`, optional (default: 30)
        If `knn` is `True`, Number of nearest neighbors in the knn graph. If
        `knn` is `False`, set the Gaussian kernel width to the distance of the
        `n_neighbors` neighbor.
    knn : `bool`, optional (default: `True`)
        If `True`, use a hard threshold to restrict the number of neighbors to
        `n_neighbors`, that is, consider a knn graph. Otherwise, use a Gaussian
        Kernel to assign low weights to neighbors more distant than the
        `n_neighbors` nearest neighbor.
    weights : subset of {{`'distances'`, `'similarities'`}} (default: {{`'distances', 'similarities'`}})
        Compute the neighborhood graph with different weights.
    n_jobs : `int` or `None` (default: `sc.settings.n_jobs`)
        Number of jobs.
    copy : `bool` (default: `False`)
        Return a copy instead of writing to adata.

    Returns
    -------
    Depending on `weights`, updates `adata` with the following:
    neighbors_similarities : sparse matrix (`adata.uns`, dtype `float32`)
        Weighted adjacency matrix of the neighborhood graph of data
        points. Weights should be interpreted as similarities.
    neighbors_distances : sparse matrix (`adata.uns`, dtype `float32`)
        Instead of decaying weights, this stores distances for each pair of
        neighbors.
    """).format(use_rep=doc_use_rep)
    logg.info('computing neighbors', r=True)
    adata = adata.copy() if copy else adata
    neighbors = Neighbors(adata)
    neighbors.compute_distances(n_neighbors=n_neighbors, knn=knn, use_rep=use_rep, umap=umap)
    if 'distances' in weights:
        adata.uns['neighbors_distances'] = neighbors.distances
    if 'similarities' in weights:
        neighbors.compute_similarities()
        adata.uns['neighbors_similarities'] = neighbors.similarities
    logg.info('    finished', time=True, end=' ' if settings.verbosity > 2 else '\n')
    hint = 'added\n'
    if 'distances' in weights:
        hint += '    \'neighbors_distances\', weighted adjacency matrix (adata.uns)'
        if 'similarities' in weights:
            hint += '\n'
    if 'similarities' in weights:
        hint += '    \'neighbors_similarities\', weighted adjacency matrix (adata.uns)'
    logg.hint(hint)
    return adata if copy else None


def get_neighbors(X, Y, k):
    Dsq = utils.comp_sqeuclidean_distance_using_matrix_mult(X, Y)
    chunk_range = np.arange(Dsq.shape[0])[:, None]
    indices_chunk = np.argpartition(Dsq, k-1, axis=1)[:, :k]
    indices_chunk = indices_chunk[chunk_range,
                                  np.argsort(Dsq[chunk_range, indices_chunk])]
    indices_chunk = indices_chunk[:, 1:]  # exclude first data point (point itself)
    distances_chunk = Dsq[chunk_range, indices_chunk]
    return indices_chunk, distances_chunk


def get_distance_matrix_and_neighbors(X, n_neighbors, sparse=True, n_jobs=1):
    """Compute distance matrix in squared Euclidian norm.
    """
    if not sparse:
        Dsq = utils.comp_sqeuclidean_distance_using_matrix_mult(X, X)
        indices, distances = get_indices_distances_of_k_nearest_from_dense_matrix(Dsq, n_neighbors)
        return Dsq, indices, distances
    # treat sparse case
    if X.shape[0] > 1e5:
        # sklearn is slower, but for large sample numbers more stable
        from sklearn.neighbors import NearestNeighbors
        sklearn_neighbors = NearestNeighbors(n_neighbors=n_neighbors-1, n_jobs=n_jobs)
        sklearn_neighbors.fit(X)
        distances, indices = sklearn_neighbors.kneighbors()
        distances = distances.astype('float32')**2
    else:
        # assume we can fit at max 20000 data points into memory
        len_chunk = np.ceil(min(20000, X.shape[0]) / n_jobs).astype(int)
        n_chunks = np.ceil(X.shape[0] / len_chunk).astype(int)
        chunks = [np.arange(start, min(start + len_chunk, X.shape[0]))
                 for start in range(0, n_chunks * len_chunk, len_chunk)]
        indices = np.zeros((X.shape[0], n_neighbors-1), dtype=int)
        distances = np.zeros((X.shape[0], n_neighbors-1), dtype=np.float32)
        if n_jobs > 1:
            # set backend threading, said to be meaningful for computations
            # with compiled code. more important: avoids hangs
            # when using Parallel below, threading is much slower than
            # multiprocessing
            result_lst = Parallel(n_jobs=n_jobs, backend='threading')(
                delayed(get_neighbors)(X[chunk], X, n_neighbors) for chunk in chunks)
        for i_chunk, chunk in enumerate(chunks):
            if n_jobs > 1:
                indices_chunk, distances_chunk = result_lst[i_chunk]
            else:
                indices_chunk, distances_chunk = get_neighbors(X[chunk], X, n_neighbors)
            indices[chunk] = indices_chunk
            distances[chunk] = distances_chunk
    Dsq = get_sparse_distance_matrix(indices, distances, X.shape[0], n_neighbors)
    return Dsq, indices, distances


def compute_knn_umap(X, n_neighbors, random_state=None,
                     metric='seuclidean', metric_kwds={}, angular=False,
                     verbose=False):
    """This is from umap [McInnes18]_.

    Given a set of data X, a neighborhood size, and a measure of distance
    compute the fuzzy simplicial set (here represented as a fuzzy graph in
    the form of a sparse matrix) associated to the data. This is done by
    locally approximating geodesic distance at each point, creating a fuzzy
    simplicial set for each such point, and then combining all the local
    fuzzy simplicial sets into a global one via a fuzzy union.
    Parameters
    ----------
    X: array of shape (n_samples, n_features)
        The data to be modelled as a fuzzy simplicial set.
    n_neighbors: int
        The number of neighbors to use to approximate geodesic distance.
        Larger numbers induce more global estimates of the manifold that can
        miss finer detail, while smaller values will focus on fine manifold
        structure to the detriment of the larger picture.
    random_state: numpy RandomState or equivalent
        A state capable being used as a numpy random state.
    metric: string or function (optional, default 'euclidean')
        The metric to use to compute distances in high dimensional space.
        If a string is passed it must match a valid predefined metric. If
        a general metric is required a function that takes two 1d arrays and
        returns a float can be provided. For performance purposes it is
        required that this be a numba jit'd function. Valid string metrics
        include:
            * euclidean
            * manhattan
            * chebyshev
            * minkowski
            * canberra
            * braycurtis
            * mahalanobis
            * wminkowski
            * seuclidean
            * cosine
            * correlation
            * haversine
            * hamming
            * jaccard
            * dice
            * russelrao
            * kulsinski
            * rogerstanimoto
            * sokalmichener
            * sokalsneath
            * yule
        Metrics that take arguments (such as minkowski, mahalanobis etc.)
        can have arguments passed via the metric_kwds dictionary. At this
        time care must be taken and dictionary elements must be ordered
        appropriately; this will hopefully be fixed in the future.
    metric_kwds: dict (optional, default {})
        Arguments to pass on to the metric, such as the ``p`` value for
        Minkowski distance.
    angular: bool (optional, default False)
        Whether to use angular/cosine distance for the random projection
        forest for seeding NN-descent to determine approximate nearest
        neighbors.
    verbose: bool (optional, default False)
        Whether to report information on the current progress of the algorithm.

    Returns
    -------
    knn_indices, knn_dists : np.arrays of shape (n_observations, n_neighbors)
    """
    from umap import sparse
    from umap.umap_ import rptree_leaf_array, make_nn_descent
    import umap.distances as dist
    import umap.sparse as sparse
    import scipy
    from sklearn.utils import check_random_state

    INT32_MIN = np.iinfo(np.int32).min + 1
    INT32_MAX = np.iinfo(np.int32).max - 1

    random_state = check_random_state(random_state)

    rows = np.zeros((X.shape[0] * n_neighbors), dtype=np.int64)
    cols = np.zeros((X.shape[0] * n_neighbors), dtype=np.int64)
    vals = np.zeros((X.shape[0] * n_neighbors), dtype=np.float64)

    if callable(metric):
        distance_func = metric
    elif metric in dist.named_distances:
        distance_func = dist.named_distances[metric]
    else:
        raise ValueError('Metric is neither callable, ' +
                         'nor a recognised string')

    if metric in ('cosine', 'correlation', 'dice', 'jaccard'):
        angular = True

    rng_state = random_state.randint(INT32_MIN, INT32_MAX, 3).astype(np.int64)

    if scipy.sparse.isspmatrix_csr(X):
        if metric in sparse.sparse_named_distances:
            distance_func = sparse.sparse_named_distances[metric]
            if metric in sparse.sparse_need_n_features:
                metric_kwds['n_features'] = X.shape[1]
        else:
            raise ValueError('Metric {} not supported for sparse ' +
                            'data'.format(metric))
        metric_nn_descent = sparse.make_sparse_nn_descent(
            distance_func, tuple(metric_kwds.values()))
        leaf_array = rptree_leaf_array(X, n_neighbors,
                                       rng_state, n_trees=10,
                                       angular=angular)
        knn_indices, knn_dists = metric_nn_descent(X.indices,
                                                   X.indptr,
                                                   X.data,
                                                   X.shape[0],
                                                   n_neighbors,
                                                   rng_state,
                                                   max_candidates=60,
                                                   rp_tree_init=True,
                                                   leaf_array=leaf_array,
                                                   verbose=verbose)
    else:
        metric_nn_descent = make_nn_descent(distance_func,
                                            tuple(metric_kwds.values()))
        # TODO: Hacked values for now
        n_trees = 5 + int(round((X.shape[0]) ** 0.5 / 20.0))
        n_iters = max(5, int(round(np.log2(X.shape[0]))))

        leaf_array = rptree_leaf_array(X, n_neighbors,
                                       rng_state, n_trees=n_trees,
                                       angular=angular)
        knn_indices, knn_dists = metric_nn_descent(X,
                                                   n_neighbors,
                                                   rng_state,
                                                   max_candidates=60,
                                                   rp_tree_init=True,
                                                   leaf_array=leaf_array,
                                                   n_iters=n_iters,
                                                   verbose=verbose)

    if np.any(knn_indices < 0):
        logg.warn('Failed to correctly find n_neighbors for some samples. '
             'Results may be less than ideal. Try re-running with '
             'different parameters.')

    # add this to avoid that points are their own neighbors
    knn_indices = knn_indices[:, 1:]
    knn_dists = knn_dists[:, 1:]
    return knn_indices, knn_dists


def get_sparse_distance_matrix(indices, distances, n_obs, n_neighbors):
    n_neighbors = n_neighbors - 1
    n_nonzero = n_obs * n_neighbors
    indptr = np.arange(0, n_nonzero + 1, n_neighbors)
    Dsq = sp.sparse.csr_matrix((distances.ravel(),
                                indices.ravel(),
                                indptr),
                                shape=(n_obs, n_obs))
    return Dsq


def get_sparse_distance_matrix_umap(indices, distances, n_obs, n_neighbors):
    n_nonzero = n_obs * n_neighbors
    indptr = np.arange(0, n_nonzero + 1, n_neighbors)
    Dsq = sp.sparse.csr_matrix((distances.ravel(),
                                indices.ravel(),
                                indptr),
                                shape=(n_obs, n_obs))
    return Dsq


def get_indices_distances_from_sparse_matrix(Dsq, n_neighbors):
    indices = np.zeros((Dsq.shape[0], n_neighbors-1), dtype=int)
    distances = np.zeros((Dsq.shape[0], n_neighbors-1), dtype=Dsq.dtype)
    for i in range(indices.shape[0]):
        neighbors = Dsq[i].nonzero()
        indices[i] = neighbors[1]
        distances[i] = Dsq[i][neighbors]
    return indices, distances


def get_indices_distances_of_k_nearest_from_dense_matrix(Dsq, k):
    sample_range = np.arange(Dsq.shape[0])[:, None]
    indices = np.argpartition(Dsq, k-1, axis=1)[:, :k]
    indices = indices[sample_range, np.argsort(Dsq[sample_range, indices])]
    indices = indices[:, 1:]  # exclude first data point (point itself)
    distances = Dsq[sample_range, indices]
    return indices, distances


def _backwards_compat_get_full_X_diffmap(adata):
    if 'X_diffmap0' in adata.obs:
        return np.c_[adata.obs['X_diffmap0'].values[:, None],
                     adata.obsm['X_diffmap'][:, :n_dcs-1]]
    else:
        return adata.obsm['X_diffmap']


def _backwards_compat_get_full_eval(adata):
    if 'X_diffmap0' in adata.obs:
        return np.r_[1, adata.uns['diffmap_evals'][:n_dcs-1]]
    else:
        return adata.uns['diffmap_evals']


class OnFlySymMatrix():
    """Emulate a matrix where elements are calculated on the fly.
    """
    def __init__(self, get_row, shape, DC_start=0, DC_end=-1, rows=None, restrict_array=None):
        self.get_row = get_row
        self.shape = shape
        self.DC_start = DC_start
        self.DC_end = DC_end
        self.rows = {} if rows is None else rows
        self.restrict_array = restrict_array  # restrict the array to a subset

    def __getitem__(self, index):
        if isinstance(index, int) or isinstance(index, np.integer):
            if self.restrict_array is None:
                glob_index = index
            else:
                # map the index back to the global index
                glob_index = self.restrict_array[index]
            if glob_index not in self.rows:
                self.rows[glob_index] = self.get_row(glob_index)
            row = self.rows[glob_index]
            if self.restrict_array is None:
                return row
            else:
                return row[self.restrict_array]
        else:
            if self.restrict_array is None:
                glob_index_0, glob_index_1 = index
            else:
                glob_index_0 = self.restrict_array[index[0]]
                glob_index_1 = self.restrict_array[index[1]]
            if glob_index_0 not in self.rows:
                self.rows[glob_index_0] = self.get_row(glob_index_0)
            return self.rows[glob_index_0][glob_index_1]

    def restrict(self, index_array):
        """Generate a view restricted to a subset of indices.
        """
        new_shape = index_array.shape[0], index_array.shape[0]
        return OnFlySymMatrix(self.get_row, new_shape, DC_start=self.DC_start,
                              DC_end=self.DC_end,
                              rows=self.rows, restrict_array=index_array)


class Neighbors():
    """Data represented as graph of nearest neighbors.

    Represent a data matrix as a graph of nearest neighbor relations (edges)
    among data points (nodes).

    Parameters
    ----------
    adata : :class:`~scanpy.api.AnnData`
        An annotated data matrix.
    n_jobs : `int` or `None` (default: `sc.settings.n_jobs`)
        Number of jobs.

    Attributes
    ----------
    distances
    similarities
    eigen_values
    eigen_basis
    laplacian

    Methods
    -------
    compute_distances
    compute_similarities
    compute_eigen
    compute_laplacian
    to_igraph
    """

    def __init__(
            self,
            adata,
            n_jobs=None):
        self._adata = adata
        self.sym = True  # we do not allow asymetric cases
        self._init_iroot()
        self.flavor = 'haghverdi16'
        # use the graph in adata
        info_str = ''
        if 'neighbors_distances' in adata.uns:
            self.knn = issparse(adata.uns['neighbors_distances'])
            self._distances = adata.uns['neighbors_distances']
            if self.knn:
                self.n_neighbors = adata.uns[
                    'neighbors_distances'][0].nonzero()[0].size + 1
            else:
                self.n_neighbors = None  # is unknown
            info_str += '`.distances` '
        else:
            self.knn = None
            self._distances = None
        # remove redundance with the previous checks...
        if 'neighbors_similarities' in adata.uns:
            self.knn = issparse(adata.uns['neighbors_similarities'])
            self._similarities = adata.uns['neighbors_similarities']
            if self.knn:
                # need to initialize again in case their were no
                # neighbors_distances
                self.n_neighbors = adata.uns[
                    'neighbors_similarities'][0].nonzero()[0].size + 1
            else:
                self.n_neighbors = None  # is unknown
            info_str += '`.similarities` '
        else:
            self._similarities = None
        if 'X_diffmap' in adata.obsm_keys():
            self.evals = _backwards_compat_get_full_eval(adata)
            self.rbasis = _backwards_compat_get_full_X_diffmap(adata)
            self.lbasis = self.rbasis
            self.n_dcs = len(self.evals)
            self.Dchosen = OnFlySymMatrix(
                self._get_Ddiff_row, shape=(self._adata.shape[0], self._adata.shape[0]))
            np.set_printoptions(precision=10)
            info_str += '`.evals` '
        else:
            self.evals = None
            self.rbasis = None
            self.lbasis = None
            self.n_dcs = None
            self.Dchosen = None
        if info_str != '':
            logg.info('    initialized {}'.format(info_str))

    @property
    def distances(self):
        """Distances between data points.

        Is a sparse matrix.
        """
        return self._distances

    @property
    def similarities(self):
        """Similarities between data points, closely related to a transition matrix.

        Is a sparse matrix.
        """
        return self._similarities

    @property
    def eigen_values(self):
        """Eigen values of similarity matrix.
        """
        return self._evals

    @property
    def eigen_basis(self):
        """Eigen basis of similarity matrix.
        """
        return self._rbasis

    @property
    def laplacian(self):
        """Graph laplacian.
        """
        return self._laplacian

    def to_igraph(self):
        """Generate igraph object.
        """
        return None

    def compute_distances(self, n_neighbors=30, knn=True, use_rep=None, umap=False):
        __doc__ = dedent("""\
        Compute distances.

        Parameters
        ----------
        n_neighbors : `int`, optional (default: 30)
             Use this number of nearest neighbors.
        knn : `bool`, optional (default: `True`)
             Restrict result to `n_neighbors` nearest neighbors.
        {use_rep}

        Returns
        -------
        Writes attribute `.distances`.
        """).format(use_rep=doc_use_rep)
        if n_neighbors > self._adata.shape[0]:  # very small datasets
            n_neighbors = 1 + int(0.5*self._adata.shape[0])
        self.n_neighbors = n_neighbors
        self.knn = knn
        X = choose_representation(self._adata, use_rep=use_rep)
        if not umap:
            self._distances, _, _ = get_distance_matrix_and_neighbors(
                X, n_neighbors, sparse=self.knn)
        else:
            # strange zero-division error...
            # doesn't appear when calling the umap tool itself
            # need to look into this...
            indices, distances = compute_knn_umap(X, n_neighbors)
            self._distances = get_sparse_distance_matrix(
                indices, distances, X.shape[0], n_neighbors)
        logg.msg('determined n_neighbors =',
            self.n_neighbors, 'nearest neighbors of each point', t=True, v=4)

    def compute_similarities(self, alpha=1):
        """Compute similarities.

        As similarities are numbers between 0 and 1, this might be interpreted
        as "transition matrix". Note, however, that if `.sym` is `True`, these
        probabilities do not sum to one, and hence, this interpretation should
        be used with care.

        Parameters
        ----------
        alpha : float
            The density rescaling parameter of Coifman and Lafon (2006). Should
            in all practical applications equal 1: Then only the geometry of the
            data matters, not the sampled density.

        Returns
        -------
        Writes attribute `.similarities`.
        """
        if self._distances is None:
            raise ValueError('You need to run `.compute_distances` first.')
        # init distances
        Dsq = self._distances
        if self.knn:
            indices, distances_sq = get_indices_distances_from_sparse_matrix(
                Dsq, self.n_neighbors)
        else:
            indices, distances_sq = get_indices_distances_of_k_nearest_from_dense_matrix(
                Dsq, self.n_neighbors)

        if self.flavor == 'unweighted':
            if not self.knn:
                raise ValueError('`flavor=\'unweighted\'` only with `knn=True`.')
            self._similarities = self._distances.sign()
            return

        # choose sigma, the heuristic here doesn't seem to make much of a difference,
        # but is used to reproduce the figures of Haghverdi et al. (2016)
        if self.knn:
            # as the distances are not sorted except for last element take
            # median
            sigmas_sq = np.median(distances_sq, axis=1)
        else:
            # the last item is already in its sorted position as argpartition
            # puts the (k-1)th element - starting to count from zero - in its
            # sorted position
            sigmas_sq = distances_sq[:, -1]/4
        sigmas = np.sqrt(sigmas_sq)

        # compute the symmetric weight matrix
        if not sp.sparse.issparse(self._distances):
            Num = 2 * np.multiply.outer(sigmas, sigmas)
            Den = np.add.outer(sigmas_sq, sigmas_sq)
            W = np.sqrt(Num/Den) * np.exp(-Dsq/Den)
            # make the weight matrix sparse
            if not self.knn:
                self.Mask = W > 1e-14
                W[self.Mask == False] = 0
            else:
                # restrict number of neighbors to ~k
                # build a symmetric mask
                Mask = np.zeros(Dsq.shape, dtype=bool)
                for i, row in enumerate(indices):
                    Mask[i, row] = True
                    for j in row:
                        if i not in set(indices[j]):
                            W[j, i] = W[i, j]
                            Mask[j, i] = True
                # set all entries that are not nearest neighbors to zero
                W[Mask == False] = 0
                self.Mask = Mask
        else:
            W = Dsq.copy()  # need to copy the distance matrix here; what follows is inplace
            for i in range(len(Dsq.indptr[:-1])):
                row = Dsq.indices[Dsq.indptr[i]:Dsq.indptr[i+1]]
                num = 2 * sigmas[i] * sigmas[row]
                den = sigmas_sq[i] + sigmas_sq[row]
                W.data[Dsq.indptr[i]:Dsq.indptr[i+1]] = np.sqrt(num/den) * np.exp(-Dsq.data[Dsq.indptr[i]: Dsq.indptr[i+1]] / den)
            W = W.tolil()
            for i, row in enumerate(indices):
                for j in row:
                    if i not in set(indices[j]):
                        W[j, i] = W[i, j]
            if False:
                W.setdiag(1)  # set diagonal to one
                logg.msg('    note that now, we set the diagonal of the weight matrix to one!')
            W = W.tocsr()
        logg.msg('computed weight matrix (W)', t=True, v=4)

        # density normalization
        # as discussed in Coifman et al. (2005)
        # ensure that kernel matrix is independent of sampling density
        if alpha == 0:
            # nothing happens here, simply use the isotropic similarity matrix
            self.K = W
        else:
            # q[i] is an estimate for the sampling density at point x_i
            # it's also the degree of the underlying graph
            if not sp.sparse.issparse(W):
                q = np.sum(W, axis=0)
                # raise to power alpha
                if alpha != 1:
                    q = q**alpha
                Den = np.outer(q, q)
                self.K = W / Den
            else:
                q = np.array(W.sum(axis=0)).flatten()
                self.K = W
                for i in range(len(W.indptr[:-1])):
                    row = W.indices[W.indptr[i]: W.indptr[i+1]]
                    num = q[i] * q[row]
                    W.data[W.indptr[i]: W.indptr[i+1]] = W.data[W.indptr[i]: W.indptr[i+1]] / num
        logg.msg('computed anisotropic kernel (K)', t=True, v=4)

        if not sp.sparse.issparse(self.K):
            # now compute the row normalization to build the transition matrix T
            # and the adjoint Ktilde: both have the same spectrum
            self.z = np.sum(self.K, axis=0)
            # the following is the transition matrix
            self.T = self.K / self.z[:, np.newaxis]
            # now we need the square root of the density
            self.sqrtz = np.array(np.sqrt(self.z))
            # now compute the density-normalized Kernel
            # it's still symmetric
            szszT = np.outer(self.sqrtz, self.sqrtz)
            self._similarities = self.K / szszT  # Ktilde
        else:
            self.z = np.array(self.K.sum(axis=0)).flatten()
            # now we need the square root of the density
            self.sqrtz = np.array(np.sqrt(self.z))
            # now compute the density-normalized Kernel
            # it's still symmetric
            self._similarities = self.K
            for i in range(len(self.K.indptr[:-1])):
                row = self.K.indices[self.K.indptr[i]: self.K.indptr[i+1]]
                num = self.sqrtz[i] * self.sqrtz[row]
                self._similarities.data[self.K.indptr[i]: self.K.indptr[i+1]] = self.K.data[self.K.indptr[i]: self.K.indptr[i+1]] / num
        logg.msg('computed similarities, the normalized anistropic kernel (Ktilde)', v=4)

    def compute_eigen(self, n_comps=15, sym=None, sort='decrease', matrix=None):
        """Compute eigen decomposition of similarity matrix.

        Parameters
        ----------
        n_comps : `int`
            Number of eigenvalues/vectors to be computed, set `n_comps = 0` if
            you need all eigenvectors.
        sym : `bool`
            Instead of computing the eigendecomposition of the assymetric
            transition matrix, computed the eigendecomposition of the symmetric
            Ktilde matrix.
        matrix : sparse matrix, np.ndarray, optional (default: `.similarities`)
            Matrix to diagonalize. Merely for testing and comparison purposes.

        Returns
        -------
        Writes the following attributes.
        evals : np.ndarray
            Eigenvalues of transition matrix
        lbasis : np.ndarray
            Matrix of left eigenvectors (stored in columns).
        rbasis : np.ndarray
             Matrix of right eigenvectors (stored in columns).
             self.rbasis is projection of data matrix on right eigenvectors,
             that is, the projection on the diffusion components.
             these are simply the components of the right eigenvectors
             and can directly be used for plotting.
        """
        np.set_printoptions(precision=10)
        if sym is None: sym = self.sym
        self.rbasisBool = True
        if matrix is None: matrix = self._similarities
        if matrix is None:
            raise ValueError('Run `.compute_similarities` first.')
        # compute the spectrum
        if n_comps == 0:
            evals, evecs = sp.linalg.eigh(matrix)
        else:
            n_comps = min(matrix.shape[0]-1, n_comps)
            # ncv = max(2 * n_comps + 1, int(np.sqrt(matrix.shape[0])))
            ncv = None
            which = 'LM' if sort == 'decrease' else 'SM'
            # it pays off to increase the stability with a bit more precision
            matrix = matrix.astype(np.float64)
            evals, evecs = sp.sparse.linalg.eigsh(matrix, k=n_comps,
                                                  which=which, ncv=ncv)
            evals, evecs = evals.astype(np.float32), evecs.astype(np.float32)
        if sort == 'decrease':
            evals = evals[::-1]
            evecs = evecs[:, ::-1]
        if logg.verbosity_greater_or_equal_than(4):
            logg.msg('computed eigenvalues', t=True, v=4)
        else:
            logg.info('    eigenvalues of transition matrix')
        logg.info('   ', str(evals).replace('\n', '\n    '))
        # assign attributes
        self.evals = evals
        count_ones = sum([1 for v in self.evals if v == 1])
        if count_ones > len(self.evals)/2:
            logg.warn('Transition matrix has many irreducible blocks!')
        if sym:
            self.rbasis = self.lbasis = evecs
        else:
            # The eigenvectors of T are stored in self.rbasis and self.lbasis
            # and are simple trafos of the eigenvectors of Ktilde.
            # rbasis and lbasis are right and left eigenvectors, respectively
            self.rbasis = np.array(evecs / self.sqrtz[:, np.newaxis])
            self.lbasis = np.array(evecs * self.sqrtz[:, np.newaxis])
            # normalize in L2 norm
            # note that, in contrast to that, a probability distribution
            # on the graph is normalized in L1 norm
            # therefore, the eigenbasis in this normalization does not correspond
            # to a probability distribution on the graph
            if False:
                self.rbasis /= np.linalg.norm(self.rbasis, axis=0, ord=2)
                self.lbasis /= np.linalg.norm(self.lbasis, axis=0, ord=2)
        # init on-the-fly computed distance "matrix"
        self.Dchosen = OnFlySymMatrix(self._get_Ddiff_row, shape=matrix.shape)

    def _init_iroot(self):
        self.iroot = None
        # set iroot directly
        if 'iroot' in self._adata.uns:
            if self._adata.uns['iroot'] >= self._adata.n_obs:
                logg.warn('Root cell index {} does not exist for {} samples. '
                          'Is ignored.'
                          .format(self._adata.uns['iroot'], self._adata.n_obs))
            else:
                self.iroot = self._adata.uns['iroot']
            return
        # set iroot via xroot
        xroot = None
        if 'xroot' in self._adata.uns: xroot = self._adata.uns['xroot']
        elif 'xroot' in self._adata.var: xroot = self._adata.var['xroot']
        # see whether we can set self.iroot using the full data matrix
        if xroot is not None and xroot.size == self._adata.shape[1]:
            self._set_iroot_via_xroot(xroot)

    def compute_laplacian(self):
        """Graph Laplacian for K.
        """
        self.L = np.diag(self.z) - self.K
        logg.info('compute graph Laplacian')

    def _update_diffmap(self, n_neighbors=30, knn=False, n_comps=None):
        """Diffusion Map as of Coifman et al. (2005) and Haghverdi et al. (2016).
        """
        if n_comps is not None:
            self.n_dcs = n_comps
            logg.info('    updating number of DCs to', self.n_dcs)
        if self.evals is None or self.evals.size < self.n_dcs:
            logg.info('    computing spectral decomposition ("diffmap") with',
                      self.n_dcs, 'components', r=True)
            self.compute_distances(n_neighbors=n_neighbors, knn=knn)
            self.compute_similarities()
            self.compute_eigen(n_comps=self.n_dcs)
            return True
        return False

    def _compute_Ddiff_all(self, n_comps=10):
        raise RuntimeError('deprecated function')
        self.embed(n_comps=n_comps)
        self._compute_M_matrix()
        self._compute_Ddiff_matrix()

    def _compute_C_all(self, n_comps=10):
        self._compute_L_matrix()
        self.embed(self.L, n_comps=n_comps, sort='increase')
        evalsL = self.evals
        self.compute_Lp_matrix()
        self.compute_C_matrix()

    def _spec_layout(self):
        self.compute_transition_matrix()
        self._compute_L_matrix()
        self.embed(self.L, sort='increase')
        # write results to dictionary
        ddmap = {}
        # skip the first eigenvalue/eigenvector
        ddmap['Y'] = self.rbasis[:, 1:]
        ddmap['evals'] = self.evals[1:]
        return ddmap

    def _get_M_row_chunk(self, i_range):
        from ..cython import utils_cy
        M_chunk = np.zeros((len(i_range), self._adata.shape[0]), dtype=np.float32)
        for i_cnt, i in enumerate(i_range):
            if False:  # not much slower, but slower
                M_chunk[i_cnt] = self.get_M_row(j)
            else:
                M_chunk[i_cnt] = utils_cy.get_M_row(i, self.evals, self.rbasis, self.lbasis)
        return M_chunk

    def _compute_M_matrix(self):
        """The M matrix is the matrix that results from summing over all powers of
        T in the subspace without the first eigenspace.

        See Haghverdi et al. (2016).
        """
        if self.n_jobs >= 4:  # if we have enough cores, skip this step
            return            # TODO: make sure that this is really the best strategy
        logg.msg('    try computing "M" matrix using up to 90% of `settings.max_memory`')
        if True:  # Python version
            self.M = sum([self.evals[l]/(1-self.evals[l])
                          * np.outer(self.rbasis[:, l], self.lbasis[:, l])
                          for l in range(1, self.evals.size)])
            self.M += np.outer(self.rbasis[:, 0], self.lbasis[:, 0])
        else:  # Cython version
            used_memory, _ = logg.get_memory_usage()
            memory_for_M = self._adata.shape[0]**2 * 23 / 8 / 1e9  # in GB
            logg.msg('    max memory =', settings.max_memory,
                   ' / used memory = {:.1f}'.format(used_memory),
                   ' / memory_for_M = {:.1f}'.format(memory_for_M))
            if used_memory + memory_for_M < 0.9 * settings.max_memory:
                logg.msg(0, '    allocate memory and compute M matrix')
                len_chunk = np.ceil(self._adata.shape[0] / self.n_jobs).astype(int)
                n_chunks = np.ceil(self._adata.shape[0] / len_chunk).astype(int)
                chunks = [np.arange(start, min(start + len_chunk, self._adata.shape[0]))
                         for start in range(0, n_chunks * len_chunk, len_chunk)]
                # parallel computing does not seem to help
                if False:  # self.n_jobs > 1:
                    # here backend threading is not necessary, and seems to slow
                    # down everything considerably
                    result_lst = Parallel(n_jobs=self.n_jobs, backend='threading')(
                        delayed(self._get_M_row_chunk)(chunk)
                        for chunk in chunks)
                self.M = np.zeros((self._adata.shape[0], self._adata.shape[0]),
                                  dtype=np.float32)
                for i_chunk, chunk in enumerate(chunks):
                    if False:  # self.n_jobs > 1:
                        M_chunk = result_lst[i_chunk]
                    else:
                        M_chunk = self._get_M_row_chunk(chunk)
                    self.M[chunk] = M_chunk
                # the following did not work
                # filename = settings.writedir + 'tmp.npy'
                # np.save(filename, self.M)
                # self.M = filename
                logg.msg(0, 'finished computation of M')
            else:
                logg.msg('not enough memory to compute M, using "on-the-fly" computation')

    def _compute_Ddiff_matrix(self):
        """Returns the distance matrix in the Diffusion Pseudotime metric.

        See Haghverdi et al. (2016).

        Notes
        -----
        - Is based on M matrix.
        - self.Ddiff[self.iroot,:] stores diffusion pseudotime as a vector.
        """
        if self.M.shape[0] > 1000:
            logg.msg('--> high number of dimensions for computing DPT distance matrix\n'
                   '    computing PCA with 50 components')
            from ..preprocessing import pca
            self.M = pca(self.M, n_comps=50, mute=True)
        self.Ddiff = sp.spatial.distance.squareform(sp.spatial.distance.pdist(self.M))
        logg.msg('computed diffusion pseudotime distance matrix', t=True)
        self.Dchosen = self.Ddiff

    def _get_Ddiff_row_chunk(self, m_i, j_range):
        from ..cython import utils_cy
        M = self.M  # caching with a file on disk did not work
        d_i = np.zeros(len(j_range))
        for j_cnt, j in enumerate(j_range):
            if False:  # not much slower, but slower
                m_j = self.get_M_row(j)
                d_i[j_cnt] = sp.spatial.distance.cdist(m_i[None, :], m_j[None, :])
            else:
                if M is None:
                    m_j = utils_cy.get_M_row(j, self.evals, self.rbasis, self.lbasis)
                else:
                    m_j = M[j]
                d_i[j_cnt] = utils_cy.c_dist(m_i, m_j)
        return d_i

    def _get_Ddiff_row(self, i):
        if not self.sym:
            raise ValueError('Not bug-free implemented! '
                             'Computation needs to be adjusted if sym=False.')
        row = sum([(self.evals[l]/(1-self.evals[l])
                     * (self.rbasis[i, l] - self.lbasis[:, l]))**2
                   # account for float32 precision
                    for l in range(0, self.evals.size) if self.evals[l] < 0.999999])
        row += sum([(self.rbasis[i, l] - self.lbasis[:, l])**2
                    for l in range(0, self.evals.size) if self.evals[l] >= 0.999999])
        return np.sqrt(row)

    def _get_Ddiff_row_deprecated(self, i):
        from ..cython import utils_cy
        if self.M is None:
            m_i = utils_cy.get_M_row(i, self.evals, self.rbasis, self.lbasis)
        else:
            m_i = self.M[i]
        len_chunk = np.ceil(self._adata.shape[0] / self.n_jobs).astype(int)
        n_chunks = np.ceil(self._adata.shape[0] / len_chunk).astype(int)
        chunks = [np.arange(start, min(start + len_chunk, self._adata.shape[0]))
                  for start in range(0, n_chunks * len_chunk, len_chunk)]
        if self.n_jobs >= 4:  # problems with high memory calculations, we skip computing M above
            # here backend threading is not necessary, and seems to slow
            # down everything considerably
            result_lst = Parallel(n_jobs=self.n_jobs)(
                delayed(self._get_Ddiff_row_chunk)(m_i, chunk)
                for chunk in chunks)
        d_i = np.zeros(self._adata.shape[0])
        for i_chunk, chunk in enumerate(chunks):
            if self.n_jobs >= 4: d_i_chunk = result_lst[i_chunk]
            else: d_i_chunk = self._get_Ddiff_row_chunk(m_i, chunk)
            d_i[chunk] = d_i_chunk
        return d_i

    def _compute_Lp_matrix(self):
        """See Fouss et al. (2006) and von Luxburg et al. (2007).

        See Proposition 6 in von Luxburg (2007) and the inline equations
        right in the text above.
        """
        self.Lp = sum([1/self.evals[i]
                      * np.outer(self.rbasis[:, i], self.lbasis[:, i])
                      for i in range(1, self.evals.size)])
        settings.mt(0, 'computed pseudoinverse of Laplacian')

    def _compute_C_matrix(self):
        """See Fouss et al. (2006) and von Luxburg et al. (2007).

        This is the commute-time matrix. It's a squared-euclidian distance
        matrix in \mathbb{R}^n.
        """
        self.C = np.repeat(np.diag(self.Lp)[:, np.newaxis],
                           self.Lp.shape[0], axis=1)
        self.C += np.repeat(np.diag(self.Lp)[np.newaxis, :],
                            self.Lp.shape[0], axis=0)
        self.C -= 2*self.Lp
        # the following is much slower
        # self.C = np.zeros(self.Lp.shape)
        # for i in range(self.Lp.shape[0]):
        #     for j in range(self.Lp.shape[1]):
        #         self.C[i, j] = self.Lp[i, i] + self.Lp[j, j] - 2*self.Lp[i, j]
        volG = np.sum(self.z)
        self.C *= volG
        settings.mt(0, 'computed commute distance matrix')
        self.Dchosen = self.C

    def _compute_MFP_matrix(self):
        """See Fouss et al. (2006).

        This is the mean-first passage time matrix. It's not a distance.

        Mfp[i, k] := m(k|i) in the notation of Fouss et al. (2006). This
        corresponds to the standard notation for transition matrices (left index
        initial state, right index final state, i.e. a right-stochastic
        matrix, with each row summing to one).
        """
        self.MFP = np.zeros(self.Lp.shape)
        for i in range(self.Lp.shape[0]):
            for k in range(self.Lp.shape[1]):
                for j in range(self.Lp.shape[1]):
                    self.MFP[i, k] += (self.Lp[i, j] - self.Lp[i, k]
                                       - self.Lp[k, j] + self.Lp[k, k]) * self.z[j]
        settings.mt(0, 'computed mean first passage time matrix')
        self.Dchosen = self.MFP

    def _set_pseudotime(self):
        """Return pseudotime with respect to root point.
        """
        self.pseudotime = self.Dchosen[self.iroot].copy()
        self.pseudotime /= np.max(self.pseudotime)

    def _set_iroot_via_xroot(self, xroot):
        """Determine the index of the root cell.

        Given an expression vector, find the observation index that is closest
        to this vector.

        Parameters
        ----------
        xroot : np.ndarray
            Vector that marks the root cell, the vector storing the initial
            condition, only relevant for computing pseudotime.
        """
        if self._adata.shape[1] != xroot.size:
            raise ValueError(
                'The root vector you provided does not have the '
                'correct dimension.')
        # this is the squared distance
        dsqroot = 1e10
        iroot = 0
        for i in range(self._adata.shape[0]):
            diff = self._adata.X[i, :] - xroot
            dsq = diff.dot(diff)
            if dsq < dsqroot:
                dsqroot = dsq
                iroot = i
                if np.sqrt(dsqroot) < 1e-10: break
        logg.msg('setting root index to', iroot, v=4)
        if self.iroot is not None and iroot != self.iroot:
            logg.warn('Changing index of iroot from {} to {}.'.format(self.iroot, iroot))
        self.iroot = iroot

    def _test_embed(self):
        """
        Checks and tests for embed.
        """
        # pl.semilogy(w,'x',label=r'$ \widetilde K$')
        # pl.show()
        if settings.verbosity > 2:
            # output of spectrum of K for comparison
            w, v = np.linalg.eigh(self.K)
            logg.msg('spectrum of K (kernel)')
        if settings.verbosity > 3:
            # direct computation of spectrum of T
            w, vl, vr = sp.linalg.eig(self.T, left=True)
            logg.msg('spectrum of transition matrix (should be same as of Ktilde)')
