"""HitTrax auto-ingest for the player-development portal.

The Railway service ``hittrax-cron`` runs::

    python hittrax/pull_export.py --commit

on cron ``15 8 * * *`` (08:15 UTC). Schedule and start command are service
instance settings. This package does not ship a railway.json.

Environment (the private key variable is ``HITTRAX_SFTP_KEY``, PEM contents;
``PRIVATE_KEY`` is not read):

    HITTRAX_SFTP_HOST
    HITTRAX_SFTP_USER
    HITTRAX_SFTP_KEY
    HITTRAX_SFTP_PORT
    HITTRAX_SFTP_PATH
    HITTRAX_SFTP_PASSWORD   optional; used only when HITTRAX_SFTP_KEY is unset
    HITTRAX_MAX_PLAYS_BYTES
    DATABASE_URL

See ``hittrax/pull_export.py`` for the contract.
"""
