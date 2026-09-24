"""Generate sample mock photos to media/samples/ for manual API trials."""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from inspections import fake_images


class Command(BaseCommand):
    help = "生成模拟环卫问题照片（原始图 + 不同角度重拍图）"

    def add_arguments(self, parser):
        parser.add_argument("--out", default=str(Path(settings.MEDIA_ROOT) / "samples"))

    def handle(self, *args, **options):
        out = Path(options["out"])
        out.mkdir(parents=True, exist_ok=True)
        for kind in fake_images.SCENES:
            original = out / f"{kind}.jpg"
            original.write_bytes(fake_images.make_photo(kind, "seed-A"))
            rephoto = out / f"{kind}_rephoto.jpg"
            rephoto.write_bytes(
                fake_images.make_rephoto(kind, "seed-A", angle=3, shift=5))
            other = out / f"{kind}_other_place.jpg"
            other.write_bytes(fake_images.make_photo(kind, "seed-A"))
            self.stdout.write(f"wrote {original.name}, {rephoto.name}, "
                              f"{other.name} (byte-identical mis-upload copy)")
        self.stdout.write(self.style.SUCCESS(f"sample photos in {out}"))
