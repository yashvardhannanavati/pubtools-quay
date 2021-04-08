import base64
from datetime import datetime
import json
import logging
import uuid

import proton

from .exceptions import SigningError
from .utils.misc import (
    run_entrypoint,
    get_internal_container_repo_name,
    log_step,
)
from .quay_api_client import QuayApiClient
from .quay_client import QuayClient
from .manifest_claims_handler import ManifestClaimsHandler

LOG = logging.getLogger("PubLogger")
logging.basicConfig()
LOG.setLevel(logging.INFO)


class SignatureHandler:
    """Base class implementing operations common for container and operator signing."""

    def __init__(self, signing_keys, hub, task_id, target_settings):
        """
        Initialize.

        Args:
            signing_keys ([str]):
                List of keys for image signing.
            hub (HubProxy):
                Instance of XMLRPC pub-hub proxy.
            task_id (str):
                task id
            target_settings (dict):
                Target settings.
        """
        self.signing_keys = signing_keys
        self.hub = hub
        self.task_id = task_id
        self.target_settings = target_settings

        # Which URL hostnames will the destination images be accessible by to customers
        self.dest_registries = target_settings["docker_settings"]["docker_reference_registry"]
        self.dest_registries = (
            self.dest_registries
            if isinstance(self.dest_registries, list)
            else [self.dest_registries]
        )

        self.quay_host = self.target_settings.get("quay_host", "quay.io").rstrip("/")
        self._quay_client = None
        self._quay_api_client = None

    @property
    def quay_client(self):
        """Create and access QuayClient."""
        if self._quay_client is None:
            self._quay_client = QuayClient(
                self.target_settings["quay_user"],
                self.target_settings["quay_password"],
                self.quay_host,
            )
        return self._quay_client

    @property
    def quay_api_client(self):
        """Create and access QuayApiClient."""
        if self._quay_api_client is None:
            self._quay_api_client = QuayApiClient(
                self.target_settings["quay_api_token"], self.quay_host
            )
        return self._quay_api_client

    def create_manifest_claim_message(
        self,
        destination_repo,
        signature_key,
        manifest_digest,
        docker_reference,
        image_name,
    ):
        """
        Construct a manifest claim (image signature) as well as a message to send to RADAS.

        Constructed signature adheres to the following standard:
        https://github.com/containers/image/blob/master/docs/containers-signature.5.md

        Args:
            destination_repo (str):
                Internal destination repository to send to RADAS.
            signature_key (str):
                Signature key that will be sent to RADAS.
            manifest_digest (str):
                Digest referencing the signed image. Mandatory part of the image signature.
            docker_reference (str):
                Image reference which will be used by customers to pull the image. Mandatory part of
                the image signature.
            image_name (str):
                Name of the image to send to RADAS.
        """
        # container image signature
        manifest_claim = {
            "critical": {
                "type": "atomic container signature",
                "image": {"docker-manifest-digest": manifest_digest},
                "identity": {"docker-reference": docker_reference},
            },
            # NOTE: pub version is no longer written here. I hope that's OK
            "optional": {"creator": "Red Hat RCM Pub"},
        }

        message = {
            "sig_key_id": signature_key,
            # Python 2.6/3 compatibility workaround
            "claim_file": base64.b64encode(json.dumps(manifest_claim).encode("latin1")).decode(
                "latin1"
            ),
            "pub_task_id": self.task_id,
            "request_id": str(uuid.uuid4()),
            "manifest_digest": manifest_digest,
            "repo": destination_repo,
            "image_name": image_name,
            "docker_reference": docker_reference,
            "created": datetime.utcnow().isoformat() + "Z",
        }
        return message

    def get_tagged_image_digests(self, image_ref):
        """
        Get all digests referenced by a tagged image.

        There will only be one digest in case of single-arch image (source image), or multiple
        digests for a multi-arch image.

        Args:
            image_ref (str):
                Image reference URL. Must be specified via tag.

        Returns ([str]):
            List of manifest digests referenced by the tag.
        """
        digests = []
        repo_path, tag = image_ref.split(":")
        repo = "/".join(repo_path.split("/")[-2:])

        repo_data = self.quay_api_client.get_repository_data(repo)

        # if 'image_id' is specified, the tag doesn't reference a ML and digest should be included
        if repo_data["tags"][tag]["image_id"]:
            digests.append(repo_data["tags"][tag]["manifest_digest"])
        # if manifest list, we want to sign only arch digests, not ML digest
        else:
            manifest_list = self.quay_client.get_manifest(image_ref, manifest_list=True)
            for manifest in manifest_list["manifests"]:
                digests.append(manifest["digest"])

        return digests

    def get_signatures_from_pyxis(self, references=None, digests=None, sig_key_ids=None):
        """
        Get existing signatures from Pyxis based on the specified criteria.

        The criteria function with an OR operator, so signatures matching any of the specified
        values will be returned.

        Args:
            references ([str]|None):
                References for which to return signatures.
            digests ([str]|None):
                Digests for which to return signatures.
            sig_key_ids ([str]|None):
                Signature keys for which to return signatures.

            Returns ([dict]):
                Existing signatures as returned by Pyxis based on specified criteria.
        """
        args = [
            "--pyxis-server",
            self.target_settings["pyxis_server"],
            "--pyxis-krb-principal",
            self.target_settings["iib_krb_principal"],
        ]
        if "iib_krb_ktfile" in self.target_settings:
            args += ["--pyxis-krb-ktfile", self.target_settings["iib_krb_ktfile"]]
        if references:
            # TODO: Should this be changed?
            args += ["--reference", ",".join(references)]
        if digests:
            args += ["--manifest-digest", ",".join(digests)]
        if sig_key_ids:
            args += ["--sig-key-id", ",".join(sig_key_ids)]

        env_vars = {}
        signatures = run_entrypoint(
            ("pubtools-pyxis", "console_scripts", "pubtools-pyxis-get-signatures"),
            "pubtools-pyxis-get-signatures",
            args,
            env_vars,
        )

        return signatures

    def filter_claim_messages(self, claim_messages):
        """
        Filter out the manifest claim messages which are already in the sigstore.

        Args:
            claim_messages ([dict]):
                Messages to be sent to RADAS.

        Returns ([dict]):
            Messages which don't yet exist in Pyxis.
        """
        LOG.info("Removing claim messages which already exist in Pyxis")
        references = [message["docker_reference"] for message in claim_messages]
        references = sorted(list(set(references)))
        digests = [message["manifest_digest"] for message in claim_messages]
        digests = sorted(list(set(digests)))

        # Signature keys are purposely omitted as too many irrelevant results might be returned
        existing_signatures = self.get_signatures_from_pyxis(references=references, digests=digests)

        signatures_by_key = {}
        for signature in existing_signatures:
            # combination of image reference, digest, and signature key makes a signature unique
            key = (signature["reference"], signature["manifest_digest"], signature["sig_key_id"])
            signatures_by_key[key] = signature

        filtered_claim_messages = []
        for message in claim_messages:
            key = (message["docker_reference"], message["manifest_digest"], message["sig_key_id"])
            if key not in signatures_by_key:
                filtered_claim_messages.append(message)

        LOG.info(
            "{0} claim messages remain after removing duplicates".format(
                len(filtered_claim_messages)
            )
        )
        return filtered_claim_messages

    def get_signatures_from_radas(self, claim_messages):
        """
        Send signature claims to RADAS via UMB and receive signed claims.

        The messaging logic is handled by the ManifestClaimsHandler class.

        Args:
            claim_messages ([dict]):
                Signature claims to be sent to RADAS.
        Returns ([dict]):
            Response messages from RADAS.
        raises MessageHandlerTimeoutException:
            If a message from RADAS hasn't arrived in time.
        """
        LOG.info("Sending claim messages to RADAS and waiting for results")
        # messages will be sent by pub-hub via XMLRPC
        # callback will be utilized by ManifestClaimsHandler, which will decide when to send msgs
        message_sender_callback = (
            lambda messages: self.hub.worker.umb_send_manifest_claim_messages(  # noqa: E731
                self.task_id, messages
            )
        )

        address = (
            "queue://Consumer.msg-producer-pub"
            ".{task_id}.VirtualTopic.eng.robosignatory.container.sign".format(task_id=self.task_id)
        )

        docker_settings = self.target_settings["docker_settings"]
        claims_handler = ManifestClaimsHandler(
            umb_urls=docker_settings["umb_urls"],
            radas_address=docker_settings.get("umb_radas_address", address),
            claim_messages=claim_messages,
            pub_cert=docker_settings.get("umb_pub_cert", "/etc/pub/umb-pub-cert-key.pem"),
            ca_cert=docker_settings.get("umb_ca_cert", "/etc/pki/tls/certs/ca-bundle.crt"),
            timeout=docker_settings.get("umb_signing_timeout", 600),
            throttle=docker_settings.get("umb_signing_throttle", 100),
            retry=docker_settings.get("umb_signing_retry", 3),
            message_sender_callback=message_sender_callback,
        )
        container = proton.reactor.Container(claims_handler)
        container.run()

        return claims_handler.received_messages

    def upload_signatures_to_pyxis(self, claim_mesages, signature_messages):
        """
        Upload signatures to Pyxis by using a pubtools-pyxis entrypoint.

        Data required for a Pyxis POST request:
        - manifest_digest
        - reference
        - repository
        - sig_key_id
        - signature_data

        Args:
            claim_messages ([dict]):
                Signature claim messages constructed for the RADAS service.
            signature_messages ([dict]):
                Messages from RADAS containing image signatures.
        """
        LOG.info("Sending new signatures to Pyxis")
        signatures = []
        claim_messages_by_id = dict((m["request_id"], m) for m in claim_mesages)
        sorted_signature_messages = sorted(signature_messages, key=lambda msg: msg["request_id"])

        for signature_message in sorted_signature_messages:
            claim_message = claim_messages_by_id[signature_message["request_id"]]

            signatures.append(
                {
                    "manifest_digest": signature_message["manifest_digest"],
                    "reference": claim_message["docker_reference"],
                    "repository": claim_message["image_name"],
                    "sig_key_id": claim_message["sig_key_id"],
                    "signature_data": signature_message["signed_claim"],
                }
            )

        args = []
        if "pyxis_server" in self.target_settings:
            args += ["--pyxis-server", self.target_settings["pyxis_server"]]
        if "iib_krb_principal" in self.target_settings:
            args += ["--pyxis-krb-principal", self.target_settings["iib_krb_principal"]]
        if "iib_krb_ktfile" in self.target_settings:
            args += ["--pyxis-krb-ktfile", self.target_settings["iib_krb_ktfile"]]
        args += ["--signatures", json.dumps(signatures)]

        env_vars = {}
        run_entrypoint(
            ("pubtools-pyxis", "console_scripts", "pubtools-pyxis-upload-signatures"),
            "pubtools-pyxis-upload-signature",
            args,
            env_vars,
        )

    def validate_radas_messages(self, claim_messages, signature_messages):
        """
        Check if messages received from RADAS contain any errors.

        Args:
            claim_messages ([dict]):
                Messages sent to RADAS.
            signature_messages ([dict]):
                Messages received from RADAS.

        Raises:
            SigningError:
                If RADAS messages contain errors.
        """
        failed_messages = 0
        for message in signature_messages:
            if message["errors"]:
                failed = [m for m in claim_messages if m["request_id"] == message["request_id"]][0]
                LOG.error(
                    "Signing of claim message {0} failed with following errors: {1}".format(
                        failed, message["errors"]
                    )
                )
                failed_messages += 1

        if failed_messages:
            raise SigningError(
                "Signing of {0}/{1} messages has failed".format(
                    failed_messages, len(claim_messages)
                )
            )


class ContainerSignatureHandler(SignatureHandler):
    """Class for handling the signing of container images."""

    def construct_item_claim_messages(self, push_item):
        """
        Construct all the signature claim messages for RADAS for one push item.

        push_item (ContainerPushItem):
            Container push item whose claim messages will be created.
        Returns ([dict]):
            Claim messages for a given push item.
        """
        LOG.info("Constructing claim messages for push item '{0}'".format(push_item))
        claim_messages = []
        digests = self.get_tagged_image_digests(push_item.metadata["pull_url"])
        # each image digest needs its own signature
        for digest in digests:
            # each destination image reference needs its own signature
            for repo, tags in sorted(push_item.metadata["tags"].items()):
                for tag in tags:
                    claim_messages += self.construct_variant_claim_messages(repo, tag, digest)

        return claim_messages

    def construct_variant_claim_messages(self, repo, tag, digest):
        """
        Construct claim messages for all specified variations of a given image.

        The variations are customer visible destination registry and signing key.

        Args:
            repo (str):
                Destination external repository  of a pushed image.
            tag: (str):
                Destination tag of a pushed image
            digest (str):
                Digest of the pushed image.

        Returns ([dict]):
            Signature claim messages to send to RADAS.
        """
        claim_messages = []
        image_schema = "{host}/{repository}:{tag}"
        internal_repo_schema = self.target_settings["quay_namespace"] + "/{internal_repo}"
        internal_repo = get_internal_container_repo_name(repo)
        dest_repo = internal_repo_schema.format(internal_repo=internal_repo)

        for registry in self.dest_registries:
            reference = image_schema.format(host=registry, repository=repo, tag=tag)

            for signing_key in self.signing_keys:
                claim_message = self.create_manifest_claim_message(
                    destination_repo=dest_repo,
                    signature_key=signing_key,
                    manifest_digest=digest,
                    docker_reference=reference,
                    image_name=repo,
                )
                claim_messages.append(claim_message)

        return claim_messages

    @log_step("Sign container images")
    def sign_container_images(self, push_items):
        """
        Perform all the steps needed to sign the images of specified push items.

        The workflow can be summarized as:
        - create manifest claim messages for all items, registries, keys, digests, repos, and tags
        - filter out requests for signatures which are already in Pyxis
        - send messages to RADAS and receive signatures (ManifestClaimsHandler class)
        - Upload new signatures to Pyxis

        Args:
            push_items (([ContainerPushItem])):
                Container push items whose images will be signed.
        """
        if not self.target_settings["docker_settings"].get(
            "docker_container_signing_enabled", False
        ):
            LOG.info("Container signing not allowed in target settings, skipping.")
            return

        claim_messages = []
        for item in push_items:
            claim_messages += self.construct_item_claim_messages(item)
        claim_messages = self.filter_claim_messages(claim_messages)
        if len(claim_messages) == 0:
            LOG.info("No new claim messages will be uploaded")
            return

        LOG.info("{0} claim messages will be uploaded".format(len(claim_messages)))
        signature_messages = self.get_signatures_from_radas(claim_messages)
        self.validate_radas_messages(claim_messages, signature_messages)
        self.upload_signatures_to_pyxis(claim_messages, signature_messages)


class OperatorSignatureHandler(SignatureHandler):
    """Class for handling the signing of index images."""

    def construct_index_image_claim_messages(self, index_image, version):
        """
        Construct signature claim messages for RADAS for the specified index image.

        index_image (str):
            Reference to a new index image constructed by IIB.
        version (str):
            Openshift version the index image was build for. Functions as an image tag.
        Returns ([dict]):
            Structured messages to be sent to UMB.
        """
        LOG.info("Constructing claim messages for index image '{0}'".format(index_image))
        claim_messages = []
        image_schema = "{host}/{repository}:{tag}"
        internal_repo_schema = self.target_settings["quay_namespace"] + "/{internal_repo}"

        # Get digests of all archs this index image was build for
        manifest_list = self.quay_client.get_manifest(index_image, manifest_list=True)
        digests = [m["digest"] for m in manifest_list["manifests"]]
        for registry in self.dest_registries:
            for signing_key in self.signing_keys:
                for digest in digests:
                    repo = self.target_settings["quay_operator_repository"]
                    internal_repo = get_internal_container_repo_name(repo)
                    dest_repo = internal_repo_schema.format(internal_repo=internal_repo)
                    reference = image_schema.format(host=registry, repository=repo, tag=version)
                    claim_message = self.create_manifest_claim_message(
                        destination_repo=dest_repo,
                        signature_key=signing_key,
                        manifest_digest=digest,
                        docker_reference=reference,
                        image_name=self.target_settings["quay_operator_repository"],
                    )
                    claim_messages.append(claim_message)

        return claim_messages

    @log_step("Sign operator images")
    def sign_operator_images(self, iib_results):
        """
        Perform all the steps needed to sign the newly constructed index images.

        Sigstore is not checked for existing signatures, as there's no way any could exist for a
        newly constructed image.

        Args:
            iib_results ({str:dict}):
                IIB results for each version the push was performed for.
        """
        if not self.target_settings["docker_settings"].get(
            "docker_container_signing_enabled", False
        ):
            LOG.info("Container signing not allowed in target settings, skipping.")
            return
        claim_messages = []

        for version, iib_result in sorted(iib_results.items()):
            claim_messages += self.construct_index_image_claim_messages(
                iib_result.index_image_resolved, version
            )
        LOG.info("claim messages: {0}".format(json.dumps(claim_messages)))
        signature_messages = self.get_signatures_from_radas(claim_messages)
        self.validate_radas_messages(claim_messages, signature_messages)

        self.upload_signatures_to_pyxis(claim_messages, signature_messages)
