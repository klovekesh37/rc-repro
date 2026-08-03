# rc-repro

rc-repro creates disposable, version-matched Rocket.Chat environments for
reproducing product behaviour and collecting evidence.

## Language

**Preset**:
A named entry in rc-repro's existing catalog that configures a reproduction. A
preset may describe a deployment type, a reproduction scenario, or both.
_Avoid_: Reproduction specification, workload scenario

**Deployment Type**:
The supported way Rocket.Chat and its required services are deployed for a
reproduction, such as the default Compose deployment, Compose multi-instance,
or Kubernetes microservices.
_Avoid_: Rendering backend, execution target, scenario

**Microservices Deployment**:
The Kubernetes microservices deployment type in rc-repro's existing preset
catalog, with the same lifecycle expectations as other deployments.
_Avoid_: Kubernetes platform, composition framework

**Reproduction Scenario**:
A product behaviour or integration context to reproduce, such as LDAP, SAML,
email, or object storage. A scenario should be reusable across compatible
deployment types rather than duplicated for each deployment.
_Avoid_: Deployment type, seed dataset, workload scenario

**Seed Dataset**:
A named initial state for a reproduction, including its intended users,
channels, and messages.
_Avoid_: Data workload, reproduction scenario, workload scenario

**Workload Scenario**:
A named pattern of test activity applied to a running reproduction, such as
logins, messaging, or a complete user journey.
_Avoid_: Seed dataset, reproduction scenario, preset

**Execution Target**:
The runtime location on which a deployment is created, such as a local
container engine, a remote container host, or a selected Kubernetes cluster.
_Avoid_: Deployment type, scenario
