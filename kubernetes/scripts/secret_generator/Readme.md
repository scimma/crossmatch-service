Secret generator
==========================

Define secrets in a file `secrets.yaml` with format:

```yaml
secrets:
  - name: test-secret
    data:
      key1: value1
      key2: value2
  ...
```

Run the secrets generator script to create Sealed Secret manifests for each secret in `secrets.yaml`:

```bash
$ python seal_secrets.py --file secrets.yaml

Sealed secret file path: "/tmp/sealed-secrets/sealed.test-secret.secret.yaml"

---
apiVersion: bitnami.com/v1alpha1
kind: SealedSecret
metadata:
  annotations:
    sealedsecrets.bitnami.com/cluster-wide: "true"
  creationTimestamp: null
  name: test-secret
spec:
  encryptedData:
    key1: AgCrkAmVM800qb...
    key2: AgAqtCF6lRT0bG...
  template:
    metadata:
      annotations:
        sealedsecrets.bitnami.com/cluster-wide: "true"
      creationTimestamp: null
      name: test-secret
    type: Opaque


```
