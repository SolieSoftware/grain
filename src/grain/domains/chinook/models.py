from typing import Optional
import datetime
import decimal

from sqlalchemy import Column, DateTime, ForeignKeyConstraint, Index, Integer, Numeric, PrimaryKeyConstraint, String, Table
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass


class Artist(Base):
    __tablename__ = 'artist'
    __table_args__ = (
        PrimaryKeyConstraint('artist_id', name='artist_pkey'),
    )

    artist_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[Optional[str]] = mapped_column(String(120))

    album: Mapped[list['Album']] = relationship('Album', back_populates='artist')


class Employee(Base):
    __tablename__ = 'employee'
    __table_args__ = (
        ForeignKeyConstraint(['reports_to'], ['employee.employee_id'], name='employee_reports_to_fkey'),
        PrimaryKeyConstraint('employee_id', name='employee_pkey'),
        Index('employee_reports_to_idx', 'reports_to')
    )

    employee_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    last_name: Mapped[str] = mapped_column(String(20), nullable=False)
    first_name: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(30))
    reports_to: Mapped[Optional[int]] = mapped_column(Integer)
    birth_date: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime)
    hire_date: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime)
    address: Mapped[Optional[str]] = mapped_column(String(70))
    city: Mapped[Optional[str]] = mapped_column(String(40))
    state: Mapped[Optional[str]] = mapped_column(String(40))
    country: Mapped[Optional[str]] = mapped_column(String(40))
    postal_code: Mapped[Optional[str]] = mapped_column(String(10))
    phone: Mapped[Optional[str]] = mapped_column(String(24))
    fax: Mapped[Optional[str]] = mapped_column(String(24))
    email: Mapped[Optional[str]] = mapped_column(String(60))

    employee: Mapped[Optional['Employee']] = relationship('Employee', remote_side=[employee_id], back_populates='employee_reverse')
    employee_reverse: Mapped[list['Employee']] = relationship('Employee', remote_side=[reports_to], back_populates='employee')
    customer: Mapped[list['Customer']] = relationship('Customer', back_populates='support_rep')


class Genre(Base):
    __tablename__ = 'genre'
    __table_args__ = (
        PrimaryKeyConstraint('genre_id', name='genre_pkey'),
    )

    genre_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[Optional[str]] = mapped_column(String(120))

    track: Mapped[list['Track']] = relationship('Track', back_populates='genre')


class MediaType(Base):
    __tablename__ = 'media_type'
    __table_args__ = (
        PrimaryKeyConstraint('media_type_id', name='media_type_pkey'),
    )

    media_type_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[Optional[str]] = mapped_column(String(120))

    track: Mapped[list['Track']] = relationship('Track', back_populates='media_type')


class Playlist(Base):
    __tablename__ = 'playlist'
    __table_args__ = (
        PrimaryKeyConstraint('playlist_id', name='playlist_pkey'),
    )

    playlist_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[Optional[str]] = mapped_column(String(120))

    track: Mapped[list['Track']] = relationship('Track', secondary='playlist_track', back_populates='playlist')


class Album(Base):
    __tablename__ = 'album'
    __table_args__ = (
        ForeignKeyConstraint(['artist_id'], ['artist.artist_id'], name='album_artist_id_fkey'),
        PrimaryKeyConstraint('album_id', name='album_pkey'),
        Index('album_artist_id_idx', 'artist_id')
    )

    album_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    artist_id: Mapped[int] = mapped_column(Integer, nullable=False)

    artist: Mapped['Artist'] = relationship('Artist', back_populates='album')
    track: Mapped[list['Track']] = relationship('Track', back_populates='album')


class Customer(Base):
    __tablename__ = 'customer'
    __table_args__ = (
        ForeignKeyConstraint(['support_rep_id'], ['employee.employee_id'], name='customer_support_rep_id_fkey'),
        PrimaryKeyConstraint('customer_id', name='customer_pkey'),
        Index('customer_support_rep_id_idx', 'support_rep_id')
    )

    customer_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    first_name: Mapped[str] = mapped_column(String(40), nullable=False)
    last_name: Mapped[str] = mapped_column(String(20), nullable=False)
    email: Mapped[str] = mapped_column(String(60), nullable=False)
    company: Mapped[Optional[str]] = mapped_column(String(80))
    address: Mapped[Optional[str]] = mapped_column(String(70))
    city: Mapped[Optional[str]] = mapped_column(String(40))
    state: Mapped[Optional[str]] = mapped_column(String(40))
    country: Mapped[Optional[str]] = mapped_column(String(40))
    postal_code: Mapped[Optional[str]] = mapped_column(String(10))
    phone: Mapped[Optional[str]] = mapped_column(String(24))
    fax: Mapped[Optional[str]] = mapped_column(String(24))
    support_rep_id: Mapped[Optional[int]] = mapped_column(Integer)

    support_rep: Mapped[Optional['Employee']] = relationship('Employee', back_populates='customer')
    invoice: Mapped[list['Invoice']] = relationship('Invoice', back_populates='customer')


class Invoice(Base):
    __tablename__ = 'invoice'
    __table_args__ = (
        ForeignKeyConstraint(['customer_id'], ['customer.customer_id'], name='invoice_customer_id_fkey'),
        PrimaryKeyConstraint('invoice_id', name='invoice_pkey'),
        Index('invoice_customer_id_idx', 'customer_id')
    )

    invoice_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    invoice_date: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    total: Mapped[decimal.Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    billing_address: Mapped[Optional[str]] = mapped_column(String(70))
    billing_city: Mapped[Optional[str]] = mapped_column(String(40))
    billing_state: Mapped[Optional[str]] = mapped_column(String(40))
    billing_country: Mapped[Optional[str]] = mapped_column(String(40))
    billing_postal_code: Mapped[Optional[str]] = mapped_column(String(10))

    customer: Mapped['Customer'] = relationship('Customer', back_populates='invoice')
    invoice_line: Mapped[list['InvoiceLine']] = relationship('InvoiceLine', back_populates='invoice')


class Track(Base):
    __tablename__ = 'track'
    __table_args__ = (
        ForeignKeyConstraint(['album_id'], ['album.album_id'], name='track_album_id_fkey'),
        ForeignKeyConstraint(['genre_id'], ['genre.genre_id'], name='track_genre_id_fkey'),
        ForeignKeyConstraint(['media_type_id'], ['media_type.media_type_id'], name='track_media_type_id_fkey'),
        PrimaryKeyConstraint('track_id', name='track_pkey'),
        Index('track_album_id_idx', 'album_id'),
        Index('track_genre_id_idx', 'genre_id'),
        Index('track_media_type_id_idx', 'media_type_id')
    )

    track_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    media_type_id: Mapped[int] = mapped_column(Integer, nullable=False)
    milliseconds: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[decimal.Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    album_id: Mapped[Optional[int]] = mapped_column(Integer)
    genre_id: Mapped[Optional[int]] = mapped_column(Integer)
    composer: Mapped[Optional[str]] = mapped_column(String(220))
    bytes: Mapped[Optional[int]] = mapped_column(Integer)

    playlist: Mapped[list['Playlist']] = relationship('Playlist', secondary='playlist_track', back_populates='track')
    album: Mapped[Optional['Album']] = relationship('Album', back_populates='track')
    genre: Mapped[Optional['Genre']] = relationship('Genre', back_populates='track')
    media_type: Mapped['MediaType'] = relationship('MediaType', back_populates='track')
    invoice_line: Mapped[list['InvoiceLine']] = relationship('InvoiceLine', back_populates='track')


class InvoiceLine(Base):
    __tablename__ = 'invoice_line'
    __table_args__ = (
        ForeignKeyConstraint(['invoice_id'], ['invoice.invoice_id'], name='invoice_line_invoice_id_fkey'),
        ForeignKeyConstraint(['track_id'], ['track.track_id'], name='invoice_line_track_id_fkey'),
        PrimaryKeyConstraint('invoice_line_id', name='invoice_line_pkey'),
        Index('invoice_line_invoice_id_idx', 'invoice_id'),
        Index('invoice_line_track_id_idx', 'track_id')
    )

    invoice_line_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(Integer, nullable=False)
    track_id: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[decimal.Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    invoice: Mapped['Invoice'] = relationship('Invoice', back_populates='invoice_line')
    track: Mapped['Track'] = relationship('Track', back_populates='invoice_line')


t_playlist_track = Table(
    'playlist_track', Base.metadata,
    Column('playlist_id', Integer, primary_key=True),
    Column('track_id', Integer, primary_key=True),
    ForeignKeyConstraint(['playlist_id'], ['playlist.playlist_id'], name='playlist_track_playlist_id_fkey'),
    ForeignKeyConstraint(['track_id'], ['track.track_id'], name='playlist_track_track_id_fkey'),
    PrimaryKeyConstraint('playlist_id', 'track_id', name='playlist_track_pkey'),
    Index('playlist_track_playlist_id_idx', 'playlist_id'),
    Index('playlist_track_track_id_idx', 'track_id')
)
