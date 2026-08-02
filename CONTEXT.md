# rc-repro

rc-repro creates disposable, version-matched Rocket.Chat environments for
reproducing product behaviour and collecting evidence.

## Language

**Reproduction Specification**:
A resolved description of the environment to create: its deployment shape,
rendering backend, reproduction scenarios, and seed dataset.
_Avoid_: Preset, workload scenario

**Deployment Shape**:
The arrangement of Rocket.Chat application processes in a reproduction, such
as single-instance, multi-instance, or microservices.
_Avoid_: Backend, execution target, topology

**Rendering Backend**:
The deployment system that materialises a reproduction specification, such as
Compose or Kubernetes.
_Avoid_: Deployment shape, execution target, topology

**Reproduction Scenario**:
A compatible product behaviour or integration context included in a
reproduction, such as LDAP, SAML, email, or object storage.
_Avoid_: Seed dataset, workload scenario, topology

**Seed Dataset**:
A named initial state for a reproduction, including its intended users,
channels, and messages.
_Avoid_: Data workload, reproduction scenario, workload scenario

**Workload Scenario**:
A named pattern of test activity applied to a running reproduction, such as
logins, messaging, or a complete user journey.
_Avoid_: Seed dataset, reproduction scenario, preset

**Execution Target**:
The runtime location on which a rendering backend operates, such as a local
container engine, a remote container host, or a selected Kubernetes cluster.
_Avoid_: Deployment shape, rendering backend, scenario

**Preset Alias**:
A legacy preset name that expands to a partial reproduction specification and
remains valid for existing users and saved records.
_Avoid_: Reproduction specification, combined preset
