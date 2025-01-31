# Multicloud Gitops

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

[Live build status](https://validatedpatterns.io/ci/?pattern=mcgitops)

## Start Here

If you've followed a link to this repository, but are not really sure what it contains
or how to use it, head over to [Multicloud GitOps](https://validatedpatterns.io/patterns/multicloud-gitops/)
for additional context and installation instructions

## Rationale

The goal for this pattern is to:

* Use a GitOps approach to manage hybrid and multi-cloud deployments across both public and private clouds.
* Enable cross-cluster governance and application lifecycle management.
* Securely manage secrets across the deployment.


## Deployment

The steps are the next:

* Copy the secrets:

    ```bash
    cp values-secret.yaml.template ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
    ```

* Install the pattern:

    ```bash
    ./pattern.sh make install
    ```

* If secrets are added/modified after installation then:

    ```bash
    cp values-secret.yaml.template ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
    ./pattern.sh make load-secrets
    ```

* Update the secrets to include the GitHub credentials and reload it:

  ```bash
  $ cat values-secret.yaml.template
    - name: rhdh-keys
      fields:
      - name: BACKEND_SECRET
        value: rhdh_1_2_3
      - name: GH_ACCESS_TOKEN
        value: XXXX
      - name: GH_CLIENT_ID
        value: XXX
      - name: GH_CLIENT_SECRET
        value: XXX

    - name: github-keys
      fields:
      - name: github_pat
        value: XXXX
      - name: webhook_secret
        value: XXXX

  # Regenerate the vault with the secrets
    cp values-secret.yaml.template ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
    ./pattern.sh make load-secrets
  ```

* And also to add the Database desired credentials for the model registry:

  ```bash
  $ cat values-secret.yaml.template
  # Database login credentials and configuration
  - name: modelregistry-keys
    fields:
    - name: database-user
      value: modelregistry_user
    - name: database-host
      value: modelregistrydb
    - name: database-db
      value: modelregistrydb
    - name: database-master-user
      value: modelregistry_user
    - name: database-password
      value: model-registry-user-123
    - name: database-root-password
      value: model-registry-user-123
    - name: database-master-password
      value: model-registry-user-123

  # Regenerate the vault with the secrets
    cp values-secret.yaml.template ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
    ./pattern.sh make load-secrets
  ```

* If your admin user do not have access to the Cluster ArgoCD, then ensure that
  the ArgoCD object have `default_policy: role:admin`, or configure the specific
  group policies as needed:

    ```bash
    oc -n openshift-gitops edit argocd  openshift-gitops
    ```


* At the Red Hat Developerhub, click on the Create, register an existing
  component and use the next url:

    ```
    #https://github.com/redhat-ai-dev/ai-lab-template/blob/main/all.yaml
    https://github.com/redhat-ai-dev/ai-lab-template/blob/release-v0.9.x/all.yaml
    https://github.com/redhat-ai-dev/model-catalog-example/blob/v0.1/ollama-model-service/catalog-info.yaml
    https://github.com/redhat-ai-dev/model-catalog-example/blob/v0.1/developer-model-service/catalog-info.yaml
    ```

* **How to enable RHDH GitHub Authentication (GH Access token, cliend_id and client_secret)**

  There are two ways you can approach setting up GitHub authentication. The simplest way (which is not recommended for production because you would not normally put tokens and secrets directly into the config) is quick and easy, but doesn’t show you how to set up a GitHub organization, which will be needed for groups of people accessing Developer Hub. To use this method, do the following:

  - Go to https://github.com and log in with your GitHub credentials.
  - Visit https://github.com/settings/tokens/new to generate a new "classic" token.
  - Now give the Token a name you will remember in the Note box.
  - Tick the Repo box to allow this token access to the repositories. Doing this allows Developer Hub to write a repo.
  - Select Generate Token and Copy the token details and save them. You will not see them again. This is your ACCESS_TOKEN.
  - Now visit https://github.com/settings/applications/new to register a new OAuth App. Make sure you have created an OAuth App, not a Github App.
  - Enter your developer hub application route URL in the Homepage URL input. This value is the default URL you are taken to when you enter Red Hat Developer Hub.
  - In the OAuth App creation page, enter a memorable name.
  - Ensure that the Callback URL in your GitHub application is configured as follows: https://redhat-developer-hub-<NAMESPACE_NAME\>.<OPENSHIFT_ROUTE_HOST\>/api/auth/github/handler/frame
  - Under Webhook, uncheck the Active box.
  - Click Create GitHub App to create the app. This will take you to the About screen.
  - Under Client Secrets, click Generate a new client secret.
  - Copy and save both CLIENT_ID and Client Secrets.

* **How to create Webhook secret and github_pat**

  - Visit https://github.com/settings/tokens/new to generate a new "classic" token.
  - Now give the Token a name you will remember in the Note box.
  - Tick the admin:redpo_hook to allow this token to manage webhooks
  - Select Generate Token and Copy the token details and save them. You will not see them again. This is your github_pat and webhook_token.