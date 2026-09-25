# AutoDOP edge BE

This directory contains the persistent configuration and data roots for the
isolated one-core edge BE used by the heterogeneous-node experiment.

The node uses ports 8043/9053/8063/9063/9073, CPU 24, and an 8 GiB memory
limit. It is separate from the three existing eight-core batch BEs. The
pipeline-driver admission limit is raised to 32768 so all fixed-DOP modes are
measured rather than rejected during the 50-query concurrency burst.
