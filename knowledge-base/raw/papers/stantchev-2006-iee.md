# Stantchev & Malek (2006) — IEE Proceedings Software
# "Architectural translucency in service-oriented architectures"
# IEE Proceedings Software, vol. 153, no. 1, pp. 31–37, 2006.
# DOI: 10.1049/ip-sen:20050017
# Captured: 2026-04-05. Immutable.

## Role in this project

Foundational theory paper. Defines "architectural translucency" as a concept
and provides the cross-layer replication performance model that underpins the
entire `pat` tool.

## Core contribution

Defines architectural translucency as the ability to monitor and control
non-functional properties (performance, reliability, security) architecture-wide
in a cross-layered way — as opposed to transparency (hiding implementation) or
opacity (no visibility at all).

Proves that the same architectural measure (replication) has different implications
on throughput ω(δ) and response time when applied at different architectural layers
in a service-oriented architecture.

## Key equations (as used in this project)

Intensity after replication at δ replicas:
```
ι(δ) = rps/δ + α·rps + β·rps·ln(δ)
```

Throughput at δ replicas:
```
ω(δ) = f(ι(δ))  — monotonically non-decreasing
```

The paper derives the cross-layer recommendation principle: always choose the layer
where the throughput gain ω(δ) is highest relative to the overhead cost (α + β·ln(δ)).

## Application to Docker/Kubernetes

The original paper uses a service-oriented architecture abstraction (SOAP/WSDL era).
The `pat` tool reapplies the same equations to Docker/Kubernetes layers
(container → pod → deployment → node), calibrating α and β for container-native overhead
profiles rather than middleware overhead.

The mathematical structure is preserved; only the layer definitions and parameter values change.

## Citation format

V. Stantchev, M. Malek, "Architectural translucency in service-oriented architectures,"
*IEE Proceedings — Software*, vol. 153, no. 1, pp. 31–37, 2006. DOI: 10.1049/ip-sen:20050017
