"""Keep personal mode unchanged; explicitly enabled multi-user mode uses the gateway."""
import os

if __name__ == "__main__":
    if os.environ.get("TINVEST_MULTIUSER") == "1":
        from tenant_gateway import main
    else:
        from demo_admin import main
    main()
